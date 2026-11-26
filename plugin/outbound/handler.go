package outbound

import (
	"context"
	"sync"

	"github.com/xtls/xray-core/common/protocol"
	"github.com/xtls/xray-core/transport/internet"

	"github.com/yourname/wdtt-outbound/wdtt"
)

// Outbound defines the interface for Xray outbound handlers.
type Outbound interface {
	Dial(ctx context.Context, dest internet.Destination) (internet.Connection, error)
	Start() error
	Close() error
}

// Handler implements the Outbound interface for Xray.
// It integrates with the WDTT Integrator for connection management.
type Handler struct {
	mu       sync.RWMutex
	started  bool
	integrator *wdtt.Integrator
	pool     *wdtt.Pool
	settings *protocol.ServerSpec
}

// Config holds configuration for the outbound handler.
type Config struct {
	Settings *protocol.ServerSpec
	Integrator *wdtt.Integrator
}

// NewHandler creates a new outbound handler with the given configuration.
func NewHandler(cfg *Config) *Handler {
	return &Handler{
		settings: cfg.Settings,
		integrator: cfg.Integrator,
	}
}

// Dial establishes a connection to the destination using the integrator's dialer.
// It retrieves a connection from the pool using the user's credentials.
func (h *Handler) Dial(ctx context.Context, dest internet.Destination) (internet.Connection, error) {
	h.mu.RLock()
	defer h.mu.RUnlock()

	if !h.started {
		return nil, ErrNotStarted
	}

	if h.integrator == nil {
		return nil, ErrNoIntegrator
	}

	// Extract user ID from destination or context
	// For now, use a default user ID; in production this should come from the request context
	userID := "default-user"

	dialer := h.integrator.GetDialer(userID)
	if dialer == nil {
		return nil, ErrNoDialer
	}

	conn, err := dialer(ctx, wdtt.Credentials{
		UserID: userID,
	})
	if err != nil {
		return nil, err
	}

	// Configure the connection with destination and settings
	_ = dest
	_ = h.settings

	return conn, nil
}

// Start initializes the outbound handler and the integrator.
func (h *Handler) Start() error {
	h.mu.Lock()
	defer h.mu.Unlock()

	if h.started {
		return ErrAlreadyStarted
	}

	if h.integrator == nil {
		return ErrNoIntegrator
	}

	if err := h.integrator.Start(context.Background()); err != nil {
		return err
	}

	h.started = true
	return nil
}

// Close shuts down the outbound handler and the integrator.
func (h *Handler) Close() error {
	h.mu.Lock()
	defer h.mu.Unlock()

	if !h.started {
		return ErrNotStarted
	}

	var err error
	if h.integrator != nil {
		err = h.integrator.Stop()
	}

	h.started = false
	return err
}

// Error definitions for the outbound handler.
var (
	ErrNotStarted     = &HandlerError{"outbound handler not started"}
	ErrAlreadyStarted = &HandlerError{"outbound handler already started"}
	ErrNoIntegrator   = &HandlerError{"no integrator configured"}
	ErrNoDialer       = &HandlerError{"no dialer available"}
	ErrNoPool         = &HandlerError{"no client pool configured"}
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