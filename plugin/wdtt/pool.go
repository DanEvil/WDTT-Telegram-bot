package wdtt

import (
	"context"
	"errors"
	"net"
	"sync"
	"sync/atomic"
	"time"
)

// Credentials represents panel credentials for per-user isolation.
type Credentials struct {
	UserID     string
	PanelToken string
	PanelURL   string
}

// Conn wraps a net.Conn with metadata for pool management.
type Conn struct {
	net.Conn
	creds      Credentials
	createdAt  time.Time
	lastUsed   atomic.Int64 // Unix nano
	inUse      atomic.Bool
	healthy    atomic.Bool
	mu         sync.Mutex
}

// PoolConfig holds configuration for the connection pool.
type PoolConfig struct {
	MaxSize             int           // Maximum number of connections in the pool
	MinIdle             int           // Minimum idle connections to maintain
	MaxIdleTime         time.Duration // Max time a connection can stay idle before being closed
	HealthCheckInterval time.Duration // Interval between health checks
	PingTimeout         time.Duration // Timeout for ping health check
	DialTimeout         time.Duration // Timeout for establishing new connections
	Dialer              func(ctx context.Context, creds Credentials) (net.Conn, error) // Custom dialer
	Pinger              func(ctx context.Context, conn net.Conn) error                // Custom ping function
}

// DefaultPoolConfig returns a sensible default configuration.
func DefaultPoolConfig() PoolConfig {
	return PoolConfig{
		MaxSize:             10,
		MinIdle:             2,
		MaxIdleTime:         5 * time.Minute,
		HealthCheckInterval: 30 * time.Second,
		PingTimeout:         5 * time.Second,
		DialTimeout:         10 * time.Second,
	}
}

// Pool manages a pool of WDTT connections per user credentials.
type Pool struct {
	config   PoolConfig
	pools    map[string]*userPool // keyed by credential hash
	poolsMu  sync.RWMutex
	closed   atomic.Bool
	wg       sync.WaitGroup
	connPool sync.Pool // Pool for Conn wrapper objects
}

// userPool holds connections for a specific user.
type userPool struct {
	creds          Credentials
	idle           chan *Conn
	sem            chan struct{} // Semaphore for limiting concurrent connections
	dialer         func(ctx context.Context) (net.Conn, error)
	pinger         func(ctx context.Context, conn net.Conn) error
	closed         atomic.Bool
	mu             sync.Mutex
	healthTicker   *time.Ticker
	maxIdleTime    time.Duration
	pingTimeout    time.Duration
	connPool       *sync.Pool // Reference to parent pool's Conn wrapper pool
}

// credKey generates a unique key for credentials.
func credKey(c Credentials) string {
	return c.UserID + "|" + c.PanelToken + "|" + c.PanelURL
}

// NewPool creates a new connection pool with the given configuration.
func NewPool(config PoolConfig) *Pool {
	if config.MaxSize <= 0 {
		config.MaxSize = 10
	}
	if config.MinIdle < 0 {
		config.MinIdle = 0
	}
	if config.MinIdle > config.MaxSize {
		config.MinIdle = config.MaxSize
	}
	if config.HealthCheckInterval <= 0 {
		config.HealthCheckInterval = 30 * time.Second
	}
	if config.PingTimeout <= 0 {
		config.PingTimeout = 5 * time.Second
	}
	if config.DialTimeout <= 0 {
		config.DialTimeout = 10 * time.Second
	}
	if config.Dialer == nil {
		config.Dialer = func(ctx context.Context, creds Credentials) (net.Conn, error) {
			return nil, errors.New("no dialer configured")
		}
	}
	if config.Pinger == nil {
		config.Pinger = defaultPinger
	}

	return &Pool{
		config: config,
		pools:  make(map[string]*userPool),
		connPool: sync.Pool{
			New: func() any {
				return &Conn{}
			},
		},
	}
}

// getOrCreateUserPool returns the user pool for the given credentials, creating it if needed.
func (p *Pool) getOrCreateUserPool(creds Credentials) *userPool {
	key := credKey(creds)

	p.poolsMu.RLock()
	up, ok := p.pools[key]
	p.poolsMu.RUnlock()

	if ok {
		return up
	}

	p.poolsMu.Lock()
	defer p.poolsMu.Unlock()

	// Double-check after acquiring write lock
	if up, ok = p.pools[key]; ok {
		return up
	}

	up = &userPool{
		creds:        creds,
		idle:         make(chan *Conn, config.MaxSize),
		sem:          make(chan struct{}, config.MaxSize),
		dialer: func(ctx context.Context) (net.Conn, error) {
			dialCtx, cancel := context.WithTimeout(ctx, config.DialTimeout)
			defer cancel()
			return config.Dialer(dialCtx, creds)
		},
		pinger:       config.Pinger,
		maxIdleTime:  config.MaxIdleTime,
		pingTimeout:  config.PingTimeout,
		connPool:     &p.connPool,
	}
	up.healthTicker = time.NewTicker(config.HealthCheckInterval)
	p.pools[key] = up

	p.wg.Add(1)
	go up.healthCheckLoop(p.wg.Done)

	// Pre-warm minimum idle connections
	for i := 0; i < config.MinIdle; i++ {
		go func() {
			ctx, cancel := context.WithTimeout(context.Background(), config.DialTimeout)
			defer cancel()
			if conn, err := up.dialer(ctx); err == nil {
				up.putIdle(&Conn{Conn: conn, creds: creds, createdAt: time.Now()})
			}
		}()
	}

	return up
}

// Get retrieves a connection from the pool for the given credentials.
// It blocks until a connection is available or the context is cancelled.
func (p *Pool) Get(ctx context.Context, creds Credentials) (net.Conn, error) {
	if p.closed.Load() {
		return nil, errors.New("pool is closed")
	}

	up := p.getOrCreateUserPool(creds)
	if up.closed.Load() {
		return nil, errors.New("user pool is closed")
	}

	// Acquire semaphore slot
	select {
	case up.sem <- struct{}{}:
	case <-ctx.Done():
		return nil, ctx.Err()
	}

	// Try to get an idle connection
	select {
	case conn := <-up.idle:
		if conn.healthy.Load() && !conn.inUse.Load() && !isStale(conn, p.config.MaxIdleTime) {
			conn.inUse.Store(true)
			conn.lastUsed.Store(time.Now().UnixNano())
			return conn, nil
		}
		// Connection is stale or unhealthy, close it and create a new one
		conn.Close()
	case <-ctx.Done():
		<-up.sem // Release semaphore
		return nil, ctx.Err()
	default:
		// No idle connections available
	}

	// Create new connection
	dialCtx, cancel := context.WithTimeout(ctx, p.config.DialTimeout)
	defer cancel()

	conn, err := up.dialer(dialCtx)
	if err != nil {
		<-up.sem // Release semaphore on failure
		return nil, err
	}

	wrapped := p.connPool.Get().(*Conn)
	wrapped.Conn = conn
	wrapped.creds = creds
	wrapped.createdAt = time.Now()
	wrapped.inUse.Store(true)
	wrapped.lastUsed.Store(time.Now().UnixNano())
	wrapped.healthy.Store(true)

	return wrapped, nil
}

// Put returns a connection to the pool.
func (p *Pool) Put(conn net.Conn) {
	wrapped, ok := conn.(*Conn)
	if !ok {
		// Not a pooled connection, just close it
		conn.Close()
		return
	}

	if p.closed.Load() {
		wrapped.Close()
		p.connPool.Put(wrapped)
		return
	}

	up := p.getOrCreateUserPool(wrapped.creds)
	if up.closed.Load() {
		wrapped.Close()
		p.connPool.Put(wrapped)
		return
	}

	wrapped.inUse.Store(false)
	wrapped.lastUsed.Store(time.Now().UnixNano())
	wrapped.Conn = nil // Clear the underlying connection

	// Return semaphore slot
	select {
	case <-up.sem:
	default:
		// Semaphore already released, connection was not from pool
	}

	// Try to return to idle pool
	select {
	case up.idle <- wrapped:
	default:
		// Pool full, close the connection wrapper
		p.connPool.Put(wrapped)
	}
}

// Close closes the entire pool and all connections.
func (p *Pool) Close() error {
	if !p.closed.CompareAndSwap(false, true) {
		return errors.New("pool already closed")
	}

	p.poolsMu.Lock()
	defer p.poolsMu.Unlock()

	for _, up := range p.pools {
		up.close()
	}
	p.pools = make(map[string]*userPool)

	p.wg.Wait()
	return nil
}

// CloseUserPool closes the pool for a specific user.
func (p *Pool) CloseUserPool(creds Credentials) error {
	key := credKey(creds)

	p.poolsMu.Lock()
	up, ok := p.pools[key]
	if ok {
		delete(p.pools, key)
	}
	p.poolsMu.Unlock()

	if ok {
		up.close()
	}
	return nil
}

// Stats returns pool statistics for a specific user.
func (p *Pool) Stats(creds Credentials) PoolStats {
	key := credKey(creds)

	p.poolsMu.RLock()
	up, ok := p.pools[key]
	p.poolsMu.RUnlock()

	if !ok {
		return PoolStats{}
	}

	return up.stats()
}

// PoolStats holds statistics for a user pool.
type PoolStats struct {
	TotalConns   int
	IdleConns    int
	InUseConns   int
	MaxSize      int
	HealthyConns int
}

// putIdle adds a connection to the idle pool.
func (up *userPool) putIdle(conn *Conn) {
	select {
	case up.idle <- conn:
	default:
		conn.Close()
		if up.connPool != nil {
			up.connPool.Put(conn)
		}
	}
}

// close closes the user pool.
func (up *userPool) close() {
	if !up.closed.CompareAndSwap(false, true) {
		return
	}

	if up.healthTicker != nil {
		up.healthTicker.Stop()
	}

	close(up.idle)
	for conn := range up.idle {
		conn.Close()
		if up.connPool != nil {
			up.connPool.Put(conn)
		}
	}

	// Drain semaphore
	for {
		select {
		case <-up.sem:
		default:
			return
		}
	}
}

// stats returns statistics for the user pool.
func (up *userPool) stats() PoolStats {
	up.mu.Lock()
	defer up.mu.Unlock()

	idleCount := len(up.idle)
	total := cap(up.idle)
	inUse := total - idleCount

	healthy := 0
	// We can't easily count healthy without iterating, so approximate
	for i := 0; i < idleCount; i++ {
		select {
		case conn := <-up.idle:
			if conn.healthy.Load() {
				healthy++
			}
			up.idle <- conn
		default:
		}
	}

	return PoolStats{
		TotalConns:   total,
		IdleConns:    idleCount,
		InUseConns:   inUse,
		MaxSize:      cap(up.sem),
		HealthyConns: healthy,
	}
}

// healthCheckLoop runs periodic health checks on idle connections.
func (up *userPool) healthCheckLoop(done func()) {
	defer done()

	for range up.healthTicker.C {
		if up.closed.Load() {
			return
		}
		up.healthCheck()
	}
}

// healthCheck performs a health check on idle connections.
func (up *userPool) healthCheck() {
	up.mu.Lock()
	defer up.mu.Unlock()

	idleCount := len(up.idle)
	if idleCount == 0 {
		return
	}

	// Check a subset of connections to avoid blocking
	checkCount := min(idleCount, 3)
	for i := 0; i < checkCount; i++ {
		select {
		case conn := <-up.idle:
			ctx, cancel := context.WithTimeout(context.Background(), up.pingTimeout)
			err := up.pinger(ctx, conn)
			cancel()

			if err != nil || isStale(conn, up.maxIdleTime) {
				conn.healthy.Store(false)
				conn.Close()
				if up.connPool != nil {
					up.connPool.Put(conn)
				}
			} else {
				conn.healthy.Store(true)
				// Put back if still healthy
				select {
				case up.idle <- conn:
				default:
					conn.Close()
					if up.connPool != nil {
						up.connPool.Put(conn)
					}
				}
			}
		default:
			return
		}
	}
}

// isStale checks if a connection has been idle too long.
func isStale(conn *Conn, maxIdleTime time.Duration) bool {
	if maxIdleTime <= 0 {
		return false
	}
	lastUsed := time.Unix(0, conn.lastUsed.Load())
	return time.Since(lastUsed) > maxIdleTime
}

// defaultPinger is a default ping implementation that sends a simple probe.
func defaultPinger(ctx context.Context, conn net.Conn) error {
	// Set a deadline for the ping
	deadline := time.Now().Add(5 * time.Second)
	if d, ok := ctx.Deadline(); ok && d.Before(deadline) {
		deadline = d
	}
	conn.SetDeadline(deadline)
	defer conn.SetDeadline(time.Time{})

	// Try to read a byte with a short timeout to detect broken connections
	// This is a generic approach; specific protocols should provide custom pingers
	buf := make([]byte, 1)
	_, err := conn.Read(buf)
	if err != nil {
		// If we got a timeout, the connection might still be alive
		if netErr, ok := err.(net.Error); ok && netErr.Timeout() {
			return nil
		}
		return err
	}
	return nil
}

// WithDialer sets a custom dialer function for the pool config.
func (c *PoolConfig) WithDialer(dialer func(ctx context.Context, creds Credentials) (net.Conn, error)) *PoolConfig {
	c.Dialer = dialer
	return c
}

// WithPinger sets a custom ping function for health checks.
func (c *PoolConfig) WithPinger(pinger func(ctx context.Context, conn net.Conn) error) *PoolConfig {
	c.Pinger = pinger
	return c
}