package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"time"

	"github.com/hashicorp/go-plugin"
	"google.golang.org/grpc"

	"github.com/pion/dtls/v2"
	"github.com/rs/zerolog"

	"github.com/xtls/xray-core/common/net"
	"github.com/xtls/xray-core/common/serial"
	"github.com/xtls/xray-core/features/outbound"
	"github.com/xtls/xray-core/transport/internet"
	"github.com/xtls/xray-core/transport/internet/stat"

	"github.com/yourname/wdtt-outbound/outbound"
	"github.com/yourname/wdtt-outbound/wdtt"
)

// PluginConfig holds the configuration for the plugin.
type PluginConfig struct {
	PanelURL            string `json:"panel_url"`
	PanelUsername       string `json:"panel_username"`
	PanelPassword       string `json:"panel_password"`
	VerifySSL           bool   `json:"verify_ssl"`
	VKHashMode          string `json:"vk_hash_mode"`         // auto, fixed, manual
	FixedVKHash         string `json:"fixed_vk_hash"`
	PoolSize            int    `json:"pool_size"`
	VKHashRefreshSec    int    `json:"vk_hash_refresh_sec"`
	WireGuardEnabled    bool   `json:"wireguard_enabled"`
	WireGuardPrivate    string `json:"wireguard_private_key"`
	WireGuardPublic     string `json:"wireguard_public_key"`
	WireGuardEndpoint   string `json:"wireguard_endpoint"`
	DialTimeoutSec      int    `json:"dial_timeout_sec"`
	// Lazy VK hash initialization service (called on first VLESS connection)
	VKHashServiceURL    string `json:"vk_hash_service_url"`    // e.g., "http://localhost:8083"
	VKHashServiceTimeout int   `json:"vk_hash_service_timeout"` // seconds
}

// HandshakeConfig is the handshake configuration for the plugin
var HandshakeConfig = plugin.HandshakeConfig{
	ProtocolVersion:  1,
	MagicCookieKey:   "XRAY_PLUGIN",
	MagicCookieValue: "wdtt-outbound",
}

// OutboundPlugin is the interface that the plugin exposes
type OutboundPlugin interface {
	Dial(ctx context.Context, dest net.Destination) (stat.Connection, error)
	Start() error
	Close() error
}

// OutboundGRPCPlugin implements plugin.GRPCPlugin for the outbound interface
type OutboundGRPCPlugin struct {
	plugin.Plugin
	Impl OutboundPlugin
}

func (p *OutboundGRPCPlugin) GRPCServer(broker *plugin.GRPCBroker, s *grpc.Server) error {
	RegisterOutboundServer(s, &outboundGRPCServer{Impl: p.Impl})
	return nil
}

func (p *OutboundGRPCPlugin) GRPCClient(ctx context.Context, broker *plugin.GRPCBroker, c *grpc.ClientConn) (interface{}, error) {
	return &outboundGRPCClient{client: NewOutboundClient(c)}, nil
}

// ============ Generated gRPC service definitions ============

// DialRequest represents a dial request
type DialRequest struct {
	Network string
	Address string
	Port    uint32
}

// DialResponse represents a dial response
type DialResponse struct {
	ConnectionId string
	Error        string
}

// StartRequest represents a start request
type StartRequest struct{}

// StartResponse represents a start response
type StartResponse struct {
	Error string
}

// CloseRequest represents a close request
type CloseRequest struct{}

// CloseResponse represents a close response
type CloseResponse struct {
	Error string
}

// OutboundServer is the server API for Outbound service
type OutboundServer interface {
	Dial(context.Context, *DialRequest) (*DialResponse, error)
	Start(context.Context, *StartRequest) (*StartResponse, error)
	Close(context.Context, *CloseRequest) (*CloseResponse, error)
	mustEmbedUnimplementedOutboundServer()
}

// UnimplementedOutboundServer must be embedded to have forward compatible implementations
type UnimplementedOutboundServer struct{}

func (UnimplementedOutboundServer) Dial(ctx context.Context, req *DialRequest) (*DialResponse, error) {
	return nil, errors.New("method Dial not implemented")
}
func (UnimplementedOutboundServer) Start(ctx context.Context, req *StartRequest) (*StartResponse, error) {
	return nil, errors.New("method Start not implemented")
}
func (UnimplementedOutboundServer) Close(ctx context.Context, req *CloseRequest) (*CloseResponse, error) {
	return nil, errors.New("method Close not implemented")
}
func (UnimplementedOutboundServer) mustEmbedUnimplementedOutboundServer() {}

// OutboundClient is the client API for Outbound service
type OutboundClient interface {
	Dial(ctx context.Context, in *DialRequest, opts ...grpc.CallOption) (*DialResponse, error)
	Start(ctx context.Context, in *StartRequest, opts ...grpc.CallOption) (*StartResponse, error)
	Close(ctx context.Context, in *CloseRequest, opts ...grpc.CallOption) (*CloseResponse, error)
}

type outboundClient struct {
	cc grpc.ClientConnInterface
}

func NewOutboundClient(cc grpc.ClientConnInterface) OutboundClient {
	return &outboundClient{cc}
}

func (c *outboundClient) Dial(ctx context.Context, in *DialRequest, opts ...grpc.CallOption) (*DialResponse, error) {
	out := new(DialResponse)
	err := c.cc.Invoke(ctx, "/outbound.Outbound/Dial", in, out, opts...)
	if err != nil {
		return nil, err
	}
	return out, nil
}

func (c *outboundClient) Start(ctx context.Context, in *StartRequest, opts ...grpc.CallOption) (*StartResponse, error) {
	out := new(StartResponse)
	err := c.cc.Invoke(ctx, "/outbound.Outbound/Start", in, out, opts...)
	if err != nil {
		return nil, err
	}
	return out, nil
}

func (c *outboundClient) Close(ctx context.Context, in *CloseRequest, opts ...grpc.CallOption) (*CloseResponse, error) {
	out := new(CloseResponse)
	err := c.cc.Invoke(ctx, "/outbound.Outbound/Close", in, out, opts...)
	if err != nil {
		return nil, err
	}
	return out, nil
}

func RegisterOutboundServer(s *grpc.Server, srv OutboundServer) {
	s.RegisterService(&_Outbound_serviceDesc, srv)
}

var _Outbound_serviceDesc = grpc.ServiceDesc{
	ServiceName: "outbound.Outbound",
	HandlerType: (*OutboundServer)(nil),
	Methods: []grpc.MethodDesc{
		{
			MethodName: "Dial",
			Handler:    _Outbound_Dial_Handler,
		},
		{
			MethodName: "Start",
			Handler:    _Outbound_Start_Handler,
		},
		{
			MethodName: "Close",
			Handler:    _Outbound_Close_Handler,
		},
	},
	Streams:  []grpc.StreamDesc{},
	Metadata: "outbound.proto",
}

func _Outbound_Dial_Handler(srv interface{}, ctx context.Context, dec func(interface{}) error, interceptor grpc.UnaryServerInterceptor) (interface{}, error) {
	in := new(DialRequest)
	if err := dec(in); err != nil {
		return nil, err
	}
	if interceptor == nil {
		return srv.(OutboundServer).Dial(ctx, in)
	}
	info := &grpc.UnaryServerInfo{
		Server:     srv,
		FullMethod: "/outbound.Outbound/Dial",
	}
	handler := func(ctx context.Context, req interface{}) (interface{}, error) {
		return srv.(OutboundServer).Dial(ctx, req.(*DialRequest))
	}
	return interceptor(ctx, in, info, handler)
}

func _Outbound_Start_Handler(srv interface{}, ctx context.Context, dec func(interface{}) error, interceptor grpc.UnaryServerInterceptor) (interface{}, error) {
	in := new(StartRequest)
	if err := dec(in); err != nil {
		return nil, err
	}
	if interceptor == nil {
		return srv.(OutboundServer).Start(ctx, in)
	}
	info := &grpc.UnaryServerInfo{
		Server:     srv,
		FullMethod: "/outbound.Outbound/Start",
	}
	handler := func(ctx context.Context, req interface{}) (interface{}, error) {
		return srv.(OutboundServer).Start(ctx, req.(*StartRequest))
	}
	return interceptor(ctx, in, info, handler)
}

func _Outbound_Close_Handler(srv interface{}, ctx context.Context, dec func(interface{}) error, interceptor grpc.UnaryServerInterceptor) (interface{}, error) {
	in := new(CloseRequest)
	if err := dec(in); err != nil {
		return nil, err
	}
	if interceptor == nil {
		return srv.(OutboundServer).Close(ctx, in)
	}
	info := &grpc.UnaryServerInfo{
		Server:     srv,
		FullMethod: "/outbound.Outbound/Close",
	}
	handler := func(ctx context.Context, req interface{}) (interface{}, error) {
		return srv.(OutboundServer).Close(ctx, req.(*CloseRequest))
	}
	return interceptor(ctx, in, info, handler)
}

// ============ Server implementation ============

type outboundGRPCServer struct {
	UnimplementedOutboundServer
	Impl OutboundPlugin
}

func (s *outboundGRPCServer) Dial(ctx context.Context, req *DialRequest) (*DialResponse, error) {
	dest := &net.Destination{
		Network: net.Network_TCP,
		Address: net.ParseAddress(req.Address),
		Port:    net.Port(req.Port),
	}
	switch req.Network {
	case "tcp":
		dest.Network = net.Network_TCP
	case "udp":
		dest.Network = net.Network_UDP
	}
	conn, err := s.Impl.Dial(ctx, dest)
	if err != nil {
		return &DialResponse{Error: err.Error()}, nil
	}
	return &DialResponse{
		ConnectionId: serial.ToString(conn.ID()),
	}, nil
}

func (s *outboundGRPCServer) Start(ctx context.Context, req *StartRequest) (*StartResponse, error) {
	err := s.Impl.Start()
	if err != nil {
		return &StartResponse{Error: err.Error()}, nil
	}
	return &StartResponse{}, nil
}

func (s *outboundGRPCServer) Close(ctx context.Context, req *CloseRequest) (*CloseResponse, error) {
	err := s.Impl.Close()
	if err != nil {
		return &CloseResponse{Error: err.Error()}, nil
	}
	return &CloseResponse{}, nil
}

// ============ Client implementation ============

type outboundGRPCClient struct {
	client OutboundClient
}

func NewOutboundClient(cc grpc.ClientConnInterface) OutboundClient {
	return &outboundClient{cc}
}

func (c *outboundClient) Dial(ctx context.Context, in *DialRequest, opts ...grpc.CallOption) (*DialResponse, error) {
	out := new(DialResponse)
	err := c.cc.Invoke(ctx, "/outbound.Outbound/Dial", in, out, opts...)
	if err != nil {
		return nil, err
	}
	return out, nil
}

func (c *outboundClient) Start(ctx context.Context, in *StartRequest, opts ...grpc.CallOption) (*StartResponse, error) {
	out := new(StartResponse)
	err := c.cc.Invoke(ctx, "/outbound.Outbound/Start", in, out, opts...)
	if err != nil {
		return nil, err
	}
	return out, nil
}

func (c *outboundClient) Close(ctx context.Context, in *CloseRequest, opts ...grpc.CallOption) (*CloseResponse, error) {
	out := new(CloseResponse)
	err := c.cc.Invoke(ctx, "/outbound.Outbound/Close", in, out, opts...)
	if err != nil {
		return nil, err
	}
	return out, nil
}

// ============ Connection wrapper ============

type grpcConnection struct {
	id     string
	client OutboundClient
}

func (c *grpcConnection) Read(b []byte) (int, error) {
	return 0, errors.New("not implemented")
}

func (c *grpcConnection) Write(b []byte) (int, error) {
	return 0, errors.New("not implemented")
}

func (c *grpcConnection) Close() error {
	return nil
}

func (c *grpcConnection) LocalAddr() net.Addr {
	return nil
}

func (c *grpcConnection) RemoteAddr() net.Addr {
	return nil
}

func (c *grpcConnection) ID() string {
	return c.id
}

// ============ Error types ============

type dialError struct{ msg string }
func (e *dialError) Error() string { return e.msg }
type startError struct{ msg string }
func (e *startError) Error() string { return e.msg }
type closeError struct{ msg string }
func (e *closeError) Error() string { return e.msg }

// ============ Plugin registration ============

// PluginMain is the main entry point for the plugin.
// It parses config from stdin, initializes all components, and serves the plugin.
func main() {
	// Parse config from stdin
	configData, err := io.ReadAll(os.Stdin)
	if err != nil {
		log.Fatalf("failed to read config from stdin: %v", err)
	}

	var pluginConfig PluginConfig
	if err := json.Unmarshal(configData, &pluginConfig); err != nil {
		log.Fatalf("failed to parse config: %v", err)
	}

	// Apply defaults
	if pluginConfig.DialTimeoutSec <= 0 {
		pluginConfig.DialTimeoutSec = 10
	}
	if pluginConfig.PoolSize <= 0 {
		pluginConfig.PoolSize = 10
	}
	if pluginConfig.VKHashRefreshSec <= 0 {
		pluginConfig.VKHashRefreshSec = 300
	}
	if pluginConfig.VKHashMode == "" {
		pluginConfig.VKHashMode = "auto"
	}

	// Setup logger
	logger := zerolog.New(os.Stderr).With().Timestamp().Logger()

	// DTLS config
	dtlsConfig := &dtls.Config{
		InsecureSkipVerify: !pluginConfig.VerifySSL,
	}

	// WireGuard config (optional)
	var wgConfig *wdtt.WireGuardConfig
	if pluginConfig.WireGuardEnabled && pluginConfig.WireGuardPrivate != "" && pluginConfig.WireGuardPublic != "" {
		wgConfig = &wdtt.WireGuardConfig{
			PrivateKey: pluginConfig.WireGuardPrivate,
			PublicKey:  pluginConfig.WireGuardPublic,
			Endpoint:   pluginConfig.WireGuardEndpoint,
			KeepAlive:  25 * time.Second,
			MTU:        1420,
		}
	}

	// VKHashManager config
	vkHashConfig := wdtt.VKHashConfig{
		Mode:           wdtt.VKHashMode(pluginConfig.VKHashMode),
		PollInterval:   time.Duration(pluginConfig.VKHashRefreshSec) * time.Second,
		FixedHash:      pluginConfig.FixedVKHash,
		PanelAPIURL:    pluginConfig.PanelURL + "/panel/api/users",
		HTTPClient:     &http.Client{Timeout: 10 * time.Second},
		TTL:            time.Duration(pluginConfig.VKHashRefreshSec) * time.Second,
	}

	// VKRelayManager config
	relayConfig := &wdtt.VKRelayManagerConfig{
		PanelURL:            pluginConfig.PanelURL,
		MaxRelaysPerUser:    4,
		RenewalBuffer:       5 * time.Minute,
		HealthCheckInterval: 30 * time.Second,
		MaxFailures:         3,
		HTTPClient:          &http.Client{Timeout: 10 * time.Second},
		Logger:              &logger,
	}

	// Pool config
		poolConfig := wdtt.PoolConfig{
			MaxSize:             pluginConfig.PoolSize,
			MinIdle:             2,
			MaxIdleTime:         5 * time.Minute,
			HealthCheckInterval: 30 * time.Second,
			PingTimeout:         5 * time.Second,
			DialTimeout:         time.Duration(pluginConfig.DialTimeoutSec) * time.Second,
		}

		// VKHashService config (lazy initialization on first VLESS connection)
		var vkHashServiceConfig *wdtt.VKHashServiceConfig
		if pluginConfig.VKHashServiceURL != "" {
			timeout := 30 * time.Second
			if pluginConfig.VKHashServiceTimeout > 0 {
				timeout = time.Duration(pluginConfig.VKHashServiceTimeout) * time.Second
			}
			vkHashServiceConfig = &wdtt.VKHashServiceConfig{
				BaseURL:    pluginConfig.VKHashServiceURL,
				HTTPClient: &http.Client{Timeout: timeout},
				Timeout:    timeout,
			}
		}

		// Integrator config
		integratorConfig := wdtt.IntegratorConfig{
			VKHashManagerConfig:   vkHashConfig,
			VKHashServiceConfig:   vkHashServiceConfig,
			VKRelayManagerConfig:  relayConfig,
			DTLSConfig:            dtlsConfig,
			DialTimeout:           time.Duration(pluginConfig.DialTimeoutSec) * time.Second,
			WireGuardConfig:       wgConfig,
			PoolConfig:            poolConfig,
		}

	// Create integrator
	integrator, err := wdtt.NewIntegrator(integratorConfig)
	if err != nil {
		log.Fatalf("failed to create integrator: %v", err)
	}

	// Create outbound handler
	handler := outbound.NewHandler(&outbound.Config{
		Settings:   &struct{}{}, // Empty settings for now
		Integrator: integrator,
	})

	// Register plugin
	plugin.Serve(&plugin.ServeConfig{
		HandshakeConfig: plugin.HandshakeConfig{
			ProtocolVersion:  1,
			MagicCookieKey:   "XRAY_PLUGIN",
			MagicCookieValue: "wdtt-outbound",
		},
		Plugins: map[string]plugin.Plugin{
			"outbound": &OutboundGRPCPlugin{Impl: handler},
		},
		GRPCServer: plugin.DefaultGRPCServer,
	})
}
