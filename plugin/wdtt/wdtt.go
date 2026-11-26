package wdtt

import (
	"github.com/pion/webrtc/v3"
	"github.com/quic-go/quic-go"
)

// TransportType represents the underlying transport type.
type TransportType string

const (
	TransportWebRTC      TransportType = "webrtc"
	TransportQUIC        TransportType = "quic"
	TransportDTLS        TransportType = "dtls"
	TransportWireGuard   TransportType = "wireguard"
)

// Session represents a WDTT session.
type Session struct {
	Transport TransportType
	WebRTC    *webrtc.PeerConnection
	QUIC      quic.Connection
}

// NewSession creates a new WDTT session.
func NewSession(transport TransportType) *Session {
	return &Session{
		Transport: transport,
	}
}