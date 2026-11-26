package outbound

import (
	"context"
	"sync"

	"github.com/xtls/xray-core/common/protocol"
	"github.com/xtls/xray-core/transport/internet"
)

// Outbound defines the interface for Xray outbound handlers.
type Outbound interface {
	Dial(ctx context.Context, dest internet.Destination) (internet.Connection, error)
	Start() error
	Close() error
}

// Handler implements the Outbound interface for Xray.
// It is designed to integrate with a WDTT client pool for connection management.
type Handler struct {
	mu       sync.RWMutex
	started  bool
	pool     ClientPool
	settings *protocol.ServerSpec
}

// ClientPool defines the interface for a WDTT client pool.
// The actual implementation will be provided by the WDTT package.
type ClientPool interface {
	Get(ctx context.Context) (internet.Connection, error)
	Put(conn internet.Connection)
	Close() error
}

// Config holds configuration for the outbound handler.
type Config struct {
	Settings *protocol.ServerSpec
	Pool     ClientPool
}

// NewHandler creates a new outbound handler with the given configuration.
func NewHandler(cfg *Config) *Handler {
	return &Handler{
		settings: cfg.Settings,
		pool:     cfg.Pool,
	}
}

// Dial establishes a connection to the destination using the client pool.
// It retrieves a connection from the pool and configures it for the destination.
func (h *Handler) Dial(ctx context.Context, dest internet.Destination) (internet.Connection, error) {
	h.mu.RLock()
	defer h.mu.RUnlock()

	if !h.started {
		return nil, ErrNotStarted
	}

	if h.pool == nil {
		return nil, ErrNoPool
	}

	conn, err := h.pool.Get(ctx)
	if err != nil {
		return nil, err
	}

	// Configure the connection with destination and settings
	// This is where destination-specific configuration would be applied
	_ = dest
	_ = h.settings

	return conn, nil
}

// Start initializes the outbound handler and prepares the client pool.
func (h *Handler) Start() error {
	h.mu.Lock()
	defer h.mu.Unlock()

	if h.started {
		return ErrAlreadyStarted
	}

	if h.pool == nil {
		return ErrNoPool
	}

	h.started = true
	return nil
}

// Close shuts down the outbound handler and releases all resources.
func (h *Handler) Close() error {
	h.mu.Lock()
	defer h.mu.Unlock()

	if !h.started {
		return ErrNotStarted
	}

	var err error
	if h.pool != nil {
		err = h.pool.Close()
	}

	h.started = false
	return err
}

// Error definitions for the outbound handler.
var (
	ErrNotStarted   = &HandlerError{"outbound handler not started"}
	ErrAlreadyStarted = &HandlerError{"outbound handler already started"}
	ErrNoPool       = &HandlerError{"no client pool configured"}
)

// HandlerError represents an error from the outbound handler.
type HandlerError struct {
	msg string
}

func (e *HandlerError) Error() string {
	return e.msg
}

// Ensure Handler implements the Outbound interface.
var _ Outbound = (*Handler)(nil)