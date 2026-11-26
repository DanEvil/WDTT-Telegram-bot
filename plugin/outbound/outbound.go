package outbound

// Outbound defines the interface for WDTT outbound connections.
type Outbound interface {
	Start() error
	Stop() error
	Dial(network, addr string) (Conn, error)
}

// Conn represents a network connection.
type Conn interface {
	Read(b []byte) (n int, err error)
	Write(b []byte) (n int, err error)
	Close() error
}