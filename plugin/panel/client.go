package panel

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/cookiejar"
	"net/url"
	"strings"
	"sync"
	"time"

	"github.com/rs/zerolog"
)

// Client is the WDTT Panel API client.
type Client struct {
	baseURL      string
	username     string
	password     string
	httpClient   *http.Client
	jar          *cookiejar.Jar
	csrfToken    string
	sessionMutex sync.RWMutex
	logger       zerolog.Logger
	retryCount   int
	retryDelay   time.Duration
}

// Config holds client configuration.
type Config struct {
	BaseURL    string
	Username   string
	Password   string
	RetryCount int
	RetryDelay time.Duration
	Logger     zerolog.Logger
}

// NewClient creates a new Panel API client.
func NewClient(cfg Config) (*Client, error) {
	if cfg.BaseURL == "" {
		return nil, fmt.Errorf("base URL is required")
	}
	if cfg.Username == "" || cfg.Password == "" {
		return nil, fmt.Errorf("username and password are required")
	}
	if cfg.RetryCount <= 0 {
		cfg.RetryCount = 3
	}
	if cfg.RetryDelay <= 0 {
		cfg.RetryDelay = 2 * time.Second
	}

	jar, err := cookiejar.New(nil)
	if err != nil {
		return nil, fmt.Errorf("create cookie jar: %w", err)
	}

	httpClient := &http.Client{
		Jar:     jar,
		Timeout: 30 * time.Second,
	}

	logger := cfg.Logger
	if logger.GetLevel() == zerolog.Disabled {
		logger = zerolog.Nop()
	}

	return &Client{
		baseURL:    strings.TrimRight(cfg.BaseURL, "/"),
		username:   cfg.Username,
		password:   cfg.Password,
		httpClient: httpClient,
		jar:        jar,
		logger:     logger,
		retryCount: cfg.RetryCount,
		retryDelay: cfg.RetryDelay,
	}, nil
}

// Login authenticates with the panel and establishes a session.
func (c *Client) Login(ctx context.Context) error {
	c.sessionMutex.Lock()
	defer c.sessionMutex.Unlock()

	loginURL := c.baseURL + "/login"
	formData := url.Values{
		"username": {c.username},
		"password": {c.password},
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, loginURL, strings.NewReader(formData.Encode()))
	if err != nil {
		return fmt.Errorf("create login request: %w", err)
	}

	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("X-Requested-With", "XMLHttpRequest")

	resp, err := c.doRequestWithRetry(ctx, req)
	if err != nil {
		return fmt.Errorf("login request failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusFound || resp.StatusCode == http.StatusUnauthorized {
		return fmt.Errorf("login failed: invalid credentials (status %d)", resp.StatusCode)
	}
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("login failed with status %d: %s", resp.StatusCode, string(body))
	}

	// Extract CSRF token from response (common patterns)
	c.extractCSRFToken(resp)

	c.logger.Info().Msg("Successfully logged in to WDTT Panel")
	return nil
}

// extractCSRFToken attempts to extract CSRF token from response headers or body.
func (c *Client) extractCSRFToken(resp *http.Response) {
	// Check for CSRF token in headers
	if token := resp.Header.Get("X-CSRF-Token"); token != "" {
		c.csrfToken = token
		return
	}
	if token := resp.Header.Get("X-Csrf-Token"); token != "" {
		c.csrfToken = token
		return
	}

	// Try to read from body if it's JSON
	contentType := resp.Header.Get("Content-Type")
	if strings.Contains(contentType, "application/json") {
		body, err := io.ReadAll(resp.Body)
		if err == nil {
			var data map[string]any
			if json.Unmarshal(body, &data) == nil {
				if token, ok := data["csrf_token"].(string); ok && token != "" {
					c.csrfToken = token
				} else if token, ok := data["_token"].(string); ok && token != "" {
					c.csrfToken = token
				}
			}
		}
	}
}

// ensureAuthenticated ensures we have a valid session, re-login if needed.
func (c *Client) ensureAuthenticated(ctx context.Context, resp *http.Response) (bool, error) {
	if resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusFound {
		c.logger.Warn().Int("status", resp.StatusCode).Msg("Session expired, re-authenticating")
		c.csrfToken = ""
		if err := c.Login(ctx); err != nil {
			return false, fmt.Errorf("re-login failed: %w", err)
		}
		return true, nil
	}
	return false, nil
}

// doRequestWithRetry executes an HTTP request with retry logic.
func (c *Client) doRequestWithRetry(ctx context.Context, req *http.Request) (*http.Response, error) {
	var lastErr error
	for attempt := 0; attempt <= c.retryCount; attempt++ {
		if attempt > 0 {
			c.logger.Debug().Int("attempt", attempt).Msg("Retrying request")
			select {
			case <-ctx.Done():
				return nil, ctx.Err()
			case <-time.After(c.retryDelay * time.Duration(attempt)):
			}
		}

		resp, err := c.httpClient.Do(req)
		if err != nil {
			lastErr = err
			continue
		}

		// Check for session expiry
		reauthenticated, err := c.ensureAuthenticated(ctx, resp)
		if err != nil {
			resp.Body.Close()
			return nil, err
		}
		if reauthenticated {
			// Retry the request with new session
			req = req.Clone(ctx)
			continue
		}

		return resp, nil
	}
	return nil, fmt.Errorf("request failed after %d retries: %w", c.retryCount, lastErr)
}

// newRequest creates a new HTTP request with common headers.
func (c *Client) newRequest(ctx context.Context, method, path string, body io.Reader) (*http.Request, error) {
	req, err := http.NewRequestWithContext(ctx, method, c.baseURL+path, body)
	if err != nil {
		return nil, err
	}

	req.Header.Set("X-Requested-With", "XMLHttpRequest")
	req.Header.Set("Accept", "application/json")

	c.sessionMutex.RLock()
	if c.csrfToken != "" {
		req.Header.Set("X-CSRF-Token", c.csrfToken)
		req.Header.Set("X-Csrf-Token", c.csrfToken)
	}
	c.sessionMutex.RUnlock()

	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}

	return req, nil
}

// getJSON performs a GET request and decodes JSON response.
func (c *Client) getJSON(ctx context.Context, path string, result any) error {
	req, err := c.newRequest(ctx, http.MethodGet, path, nil)
	if err != nil {
		return err
	}

	resp, err := c.doRequestWithRetry(ctx, req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	return c.decodeResponse(resp, result)
}

// postJSON performs a POST request with JSON body and decodes JSON response.
func (c *Client) postJSON(ctx context.Context, path string, payload, result any) error {
	body, err := json.Marshal(payload)
	if err != nil {
		return fmt.Errorf("marshal payload: %w", err)
	}

	req, err := c.newRequest(ctx, http.MethodPost, path, bytes.NewReader(body))
	if err != nil {
		return err
	}

	resp, err := c.doRequestWithRetry(ctx, req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	return c.decodeResponse(resp, result)
}

// decodeResponse decodes JSON response into result, handling errors.
func (c *Client) decodeResponse(resp *http.Response, result any) error {
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return fmt.Errorf("read response body: %w", err)
	}

	if resp.StatusCode >= 400 {
		var apiErr APIError
		if json.Unmarshal(body, &apiErr) == nil && apiErr.Message != "" {
			return apiErr
		}
		return fmt.Errorf("API error (status %d): %s", resp.StatusCode, string(body))
	}

	if result == nil {
		return nil
	}

	if err := json.Unmarshal(body, result); err != nil {
		return fmt.Errorf("unmarshal response: %w (body: %s)", err, string(body))
	}

	return nil
}

// APIError represents an API error response.
type APIError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
	Details string `json:"details,omitempty"`
}

func (e APIError) Error() string {
	if e.Details != "" {
		return fmt.Sprintf("API error %d: %s (%s)", e.Code, e.Message, e.Details)
	}
	return fmt.Sprintf("API error %d: %s", e.Code, e.Message)
}

// User represents a panel user.
type User struct {
	ID        int    `json:"id"`
	Username  string `json:"username"`
	Email     string `json:"email"`
	Role      string `json:"role"`
	Status    string `json:"status"`
	CreatedAt string `json:"created_at"`
	UpdatedAt string `json:"updated_at"`
}

// UsersResponse represents the response from get_users.
type UsersResponse struct {
	Users []User `json:"users"`
	Total int    `json:"total"`
	Page  int    `json:"page"`
	Limit int    `json:"limit"`
}

// GetUsers retrieves the list of users with pagination.
func (c *Client) GetUsers(ctx context.Context, page, limit int) (*UsersResponse, error) {
	if page <= 0 {
		page = 1
	}
	if limit <= 0 {
		limit = 50
	}

	path := fmt.Sprintf("/api/users?page=%d&limit=%d", page, limit)
	var result UsersResponse
	if err := c.getJSON(ctx, path, &result); err != nil {
		return nil, fmt.Errorf("get users: %w", err)
	}
	return &result, nil
}

// UserInfo represents detailed user information.
type UserInfo struct {
	User
	LastLogin  string `json:"last_login"`
	IPAddress  string `json:"ip_address"`
	UserAgent  string `json:"user_agent"`
	Balance    int64  `json:"balance"`
	Minutes    int    `json:"minutes"`
	ExpiresAt  string `json:"expires_at"`
	Extensions []struct {
		Number    string `json:"number"`
		Enabled   bool   `json:"enabled"`
		Status    string `json:"status"`
		CreatedAt string `json:"created_at"`
	} `json:"extensions"`
}

// GetUserInfo retrieves detailed information for a specific user.
func (c *Client) GetUserInfo(ctx context.Context, userID int) (*UserInfo, error) {
	path := fmt.Sprintf("/api/users/%d", userID)
	var result UserInfo
	if err := c.getJSON(ctx, path, &result); err != nil {
		return nil, fmt.Errorf("get user info: %w", err)
	}
	return &result, nil
}

// VKStatus represents VK integration status.
type VKStatus struct {
	Connected    bool   `json:"connected"`
	AccountID    int64  `json:"account_id"`
	AccountName  string `json:"account_name"`
	TokenExpires string `json:"token_expires"`
	Scopes       string `json:"scopes"`
	LastSync     string `json:"last_sync"`
	Error        string `json:"error,omitempty"`
}

// GetVKStatus retrieves the VK integration status.
func (c *Client) GetVKStatus(ctx context.Context) (*VKStatus, error) {
	var result VKStatus
	if err := c.getJSON(ctx, "/api/vk/status", &result); err != nil {
		return nil, fmt.Errorf("get VK status: %w", err)
	}
	return &result, nil
}

// VKCall represents a VK call record.
type VKCall struct {
	ID           int64  `json:"id"`
	CallID       string `json:"call_id"`
	Direction    string `json:"direction"` // "inbound" or "outbound"
	FromNumber   string `json:"from_number"`
	ToNumber     string `json:"to_number"`
	Status       string `json:"status"` // "answered", "missed", "busy", "failed"
	Duration     int    `json:"duration"` // in seconds
	RecordingURL string `json:"recording_url,omitempty"`
	Cost         int64  `json:"cost"` // in kopecks
	StartedAt    string `json:"started_at"`
	AnsweredAt   string `json:"answered_at,omitempty"`
	EndedAt      string `json:"ended_at,omitempty"`
	CreatedAt    string `json:"created_at"`
}

// VKCallsResponse represents the response from vk_calls.
type VKCallsResponse struct {
	Calls []VKCall `json:"calls"`
	Total int      `json:"total"`
	Page  int      `json:"page"`
	Limit int      `json:"limit"`
}

// GetVKCalls retrieves VK call history with pagination and filters.
func (c *Client) GetVKCalls(ctx context.Context, page, limit int, filters map[string]string) (*VKCallsResponse, error) {
	if page <= 0 {
		page = 1
	}
	if limit <= 0 {
		limit = 50
	}

	params := url.Values{}
	params.Set("page", fmt.Sprintf("%d", page))
	params.Set("limit", fmt.Sprintf("%d", limit))
	for k, v := range filters {
		params.Set(k, v)
	}

	path := fmt.Sprintf("/api/vk/calls?%s", params.Encode())
	var result VKCallsResponse
	if err := c.getJSON(ctx, path, &result); err != nil {
		return nil, fmt.Errorf("get VK calls: %w", err)
	}
	return &result, nil
}

// CreateVKCallRequest represents the request to create a VK call.
type CreateVKCallRequest struct {
	ToNumber   string `json:"to_number"`
	FromNumber string `json:"from_number,omitempty"`
	CallerID   string `json:"caller_id,omitempty"`
	Timeout    int    `json:"timeout,omitempty"` // in seconds
	Record     bool   `json:"record,omitempty"`
}

// CreateVKCallResponse represents the response from create_vk_call.
type CreateVKCallResponse struct {
	CallID     string `json:"call_id"`
	Status     string `json:"status"`
	Message    string `json:"message,omitempty"`
	Estimated  int    `json:"estimated_cost,omitempty"` // in kopecks
	StartedAt  string `json:"started_at"`
}

// CreateVKCall initiates a new VK call.
func (c *Client) CreateVKCall(ctx context.Context, req CreateVKCallRequest) (*CreateVKCallResponse, error) {
	var result CreateVKCallResponse
	if err := c.postJSON(ctx, "/api/vk/calls", req, &result); err != nil {
		return nil, fmt.Errorf("create VK call: %w", err)
	}
	return &result, nil
}

// HealthCheck verifies the panel API is reachable and authenticated.
func (c *Client) HealthCheck(ctx context.Context) error {
	req, err := c.newRequest(ctx, http.MethodGet, "/api/health", nil)
	if err != nil {
		return err
	}

	resp, err := c.doRequestWithRetry(ctx, req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("health check failed: status %d", resp.StatusCode)
	}
	return nil
}

// Close closes the client and cleans up resources.
func (c *Client) Close() error {
	c.sessionMutex.Lock()
	defer c.sessionMutex.Unlock()
	c.csrfToken = ""
	c.jar = nil
	return nil
}