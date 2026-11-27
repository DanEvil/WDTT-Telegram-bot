// wdtt-socks — headless WDTT-клиент (VK TURN) с локальным SOCKS5.
//
// Режим run: поднимает ядро wg-turn-client (RAW-туннель через VK TURN),
// TUN в отдельном netns с default-маршрутом через туннель, veth в хост,
// и SOCKS5-сервер внутри netns. xray ходит в этот SOCKS — весь его
// outbound идёт через WDTT/VK TURN (обход белых списков).
//
//	wdtt-socks run -link 'wdtt://host:56000:56001:9000:pass:hash1,hash2'
//	wdtt-socks socks -addr 10.255.254.2:1080
package main

import (
	"context"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"os"
	"os/exec"
	"os/signal"
	"strconv"
	"strings"
	"sync/atomic"
	"syscall"
	"time"

	"golang.zx2c4.com/wireguard/tun"
	"wg-turn-client/core"
)

const (
	defaultNS     = "wdttx"
	defaultTun    = "wdttx0"
	defaultListen = "127.0.0.1:19000"
	vethHost      = "veth-wdttx-h"
	vethNS        = "veth-wdttx-n"
	hostVethIP    = "10.255.254.1/24"
	nsVethIP      = "10.255.254.2/24"
)

type linkInfo struct {
	IP       string
	DtlsPort string
	RawPort  string
	Password string
	Hashes   []string
}

func parseLink(link string) (linkInfo, error) {
	link = strings.TrimSpace(link)
	if !strings.HasPrefix(link, "wdtt://") {
		return linkInfo{}, fmt.Errorf("need wdtt:// link")
	}
	payload := strings.TrimPrefix(link, "wdtt://")
	if i := strings.IndexByte(payload, '#'); i >= 0 {
		payload = payload[:i]
	}
	parts := strings.Split(payload, ":")
	if len(parts) >= 5 && !strings.Contains(payload, "=") && isDigits(parts[1]) {
		li := linkInfo{IP: parts[0], DtlsPort: parts[1], Password: parts[4]}
		if len(parts) > 2 {
			li.RawPort = parts[2]
		}
		if len(parts) > 5 && parts[5] != "" {
			for _, h := range strings.Split(parts[5], ",") {
				if t := strings.TrimSpace(h); t != "" {
					li.Hashes = append(li.Hashes, t)
				}
			}
		}
		if li.IP == "" || li.DtlsPort == "" || li.Password == "" {
			return linkInfo{}, fmt.Errorf("incomplete colon wdtt link")
		}
		return li, nil
	}
	// base64 JSON
	data, err := base64.StdEncoding.DecodeString(payload)
	if err != nil {
		data, err = base64.RawStdEncoding.DecodeString(payload)
		if err != nil {
			return linkInfo{}, fmt.Errorf("not colon, not base64: %w", err)
		}
	}
	var raw map[string]interface{}
	if err := json.Unmarshal(data, &raw); err != nil {
		return linkInfo{}, fmt.Errorf("bad json link: %w", err)
	}
	first := func(keys ...string) string {
		for _, k := range keys {
			if v, ok := raw[k]; ok {
				if s := strings.TrimSpace(fmt.Sprint(v)); s != "" {
					return s
				}
			}
		}
		return ""
	}
	num := func(keys ...string) string {
		for _, k := range keys {
			if v, ok := raw[k]; ok {
				switch n := v.(type) {
				case float64:
					return strconv.FormatInt(int64(n), 10)
				case json.Number:
					return n.String()
				}
			}
		}
		return ""
	}
	li := linkInfo{
		IP:       first("ip", "add"),
		DtlsPort: num("dtls"),
		RawPort:  num("raw"),
		Password: first("pass", "id"),
	}
	if h := first("hash", "vk_hash"); h != "" {
		for _, x := range strings.Split(h, ",") {
			if t := strings.TrimSpace(x); t != "" {
				li.Hashes = append(li.Hashes, t)
			}
		}
	}
	if li.IP == "" || li.DtlsPort == "" || li.Password == "" {
		return linkInfo{}, fmt.Errorf("incomplete json wdtt link: ip=%q dtls=%q", li.IP, li.DtlsPort)
	}
	return li, nil
}

func isDigits(s string) bool {
	if s == "" {
		return false
	}
	for _, c := range s {
		if c < '0' || c > '9' {
			return false
		}
	}
	return true
}

func run(args ...string) error {
	cmd := exec.Command(args[0], args[1:]...)
	out, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("%v: %s (%w)", args, strings.TrimSpace(string(out)), err)
	}
	return nil
}

func mustPort(s string) int {
	_, p, _ := net.SplitHostPort(s)
	n, _ := strconv.Atoi(p)
	return n
}

func main() {
	log.SetFlags(log.LstdFlags | log.Lmicroseconds)
	if len(os.Args) > 1 && os.Args[1] == "socks" {
		socksMain()
		return
	}
	if len(os.Args) < 2 {
		fmt.Println("usage: wdtt-socks run -link wdtt://... | wdtt-socks socks -addr host:port")
		os.Exit(2)
	}
	runMain()
}

func runMain() {
	runCmd := flag.NewFlagSet("run", flag.ExitOnError)
	link := runCmd.String("link", "", "wdtt:// link")
	cfgRoot := runCmd.String("cfg-root", "/var/lib/wdtt-socks", "core config root")
	ns := runCmd.String("ns", defaultNS, "netns name")
	tunName := runCmd.String("tun", defaultTun, "tun iface name")
	listen := runCmd.String("listen", defaultListen, "core local UDP listen")
	socksAddr := runCmd.String("socks-addr", "10.255.254.2:1080", "SOCKS5 addr inside netns")
	workers := runCmd.Int("workers", 9, "turn workers")
	runCmd.Parse(os.Args[2:])

	if *link == "" {
		log.Fatal("need -link wdtt://...")
	}
	li, err := parseLink(*link)
	if err != nil {
		log.Fatalf("parse link: %v", err)
	}
	if len(li.Hashes) == 0 {
		log.Fatal("link has no vk hashes")
	}
	log.Printf("link: peer=%s:%s raw=%s hashes=%d", li.IP, li.DtlsPort, li.RawPort, len(li.Hashes))

	core.SetConfigRoot(*cfgRoot)
	// parts[2] в colon-ссылке — это wg_port, а не raw. Direct RAW живёт на DTLS+3,
	// поэтому RawDirectPort=0 (авто) — см. rawDirectPeerAddr в ядре.
	_ = li.RawPort
	peer := net.JoinHostPort(li.IP, li.DtlsPort)
	cfg := core.Config{
		PeerAddr:      peer,
		Password:      li.Password,
		Hashes:        li.Hashes,
		Listen:        *listen,
		DeviceID:      "wdtt-socks-exit",
		Workers:       *workers,
		MTU:           1280,
		ObfsMode:      "audio",
		TunnelMode:    "raw",
		TurnTransport: "tcp",
		RawDirectPort: 0,
	}
	c := core.New(cfg)
	events, err := c.Start()
	if err != nil {
		log.Fatalf("core start: %v", err)
	}
	defer c.Stop()

	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()

	ready := make(chan string, 1)
	go func() {
		for ev := range events {
			switch ev.Type {
			case core.EventLog:
				if strings.Contains(ev.Message, "READY") || strings.Contains(ev.Message, "TURN ") ||
					strings.Contains(ev.Message, "ошиб") || strings.Contains(ev.Message, "error") {
					log.Printf("[%s] %s", ev.Level, ev.Message)
				}
			case core.EventError:
				log.Printf("[ERR] %s", ev.Message)
			case core.EventEvent:
				if ev.Name == "raw_config" {
					select {
					case ready <- ev.Data:
					default:
					}
				}
			case core.EventState:
				log.Printf("[STATE] %s", ev.Status)
			}
		}
	}()

	var conf string
	select {
	case conf = <-ready:
	case <-time.After(120 * time.Second):
		log.Fatal("timeout waiting raw_config")
	case <-ctx.Done():
		return
	}
	ip, mtu := parseRawConf(conf)
	if ip == "" {
		log.Fatal("no IP in raw_config")
	}
	log.Printf("raw_config: ip=%s mtu=%d", ip, mtu)

	dev, err := tun.CreateTUN(*tunName, mtu)
	if err != nil {
		log.Fatalf("create tun: %v", err)
	}
	if err := setupNetns(*ns, *tunName, ip, mtu); err != nil {
		_ = dev.Close()
		log.Fatalf("netns setup: %v", err)
	}
	if err := startBridge(dev, *listen); err != nil {
		_ = dev.Close()
		log.Fatalf("bridge: %v", err)
	}
	defer stopBridge()
	c.NotifyTunReady()

	// SOCKS5 внутри netns (его dial идёт по default-маршруту через TUN)
	self, _ := os.Executable()
	socksCmd := exec.Command("ip", "netns", "exec", *ns, self, "socks", "-addr", *socksAddr)
	socksCmd.Stdout = os.Stdout
	socksCmd.Stderr = os.Stderr
	if err := socksCmd.Start(); err != nil {
		log.Fatalf("start socks: %v", err)
	}
	defer func() { _ = socksCmd.Process.Kill() }()
	go func() {
		if err := socksCmd.Wait(); err != nil {
			log.Printf("[SOCKS] exited: %v", err)
		}
	}()

	// ждём, пока socks поднимется
	for i := 0; i < 50; i++ {
		if conn, err := net.DialTimeout("tcp", *socksAddr, 500*time.Millisecond); err == nil {
			_ = conn.Close()
			break
		}
		time.Sleep(200 * time.Millisecond)
	}
	log.Printf("READY ns=%s tun=%s ip=%s socks=%s (via %s)", *ns, *tunName, ip, *socksAddr, *listen)
	fmt.Printf("WDTT_SOCKS_READY ns=%s ip=%s socks=%s\n", *ns, ip, *socksAddr)

	<-ctx.Done()
	log.Printf("shutting down")
}

func setupNetns(ns, tunName, ip string, mtu int) error {
	_ = run("ip", "netns", "del", ns)
	if err := run("ip", "netns", "add", ns); err != nil {
		return err
	}
	if err := run("ip", "link", "set", tunName, "netns", ns); err != nil {
		return err
	}
	// veth host <-> ns
	_ = run("ip", "link", "del", vethHost)
	if err := run("ip", "link", "add", vethHost, "type", "veth", "peer", "name", vethNS); err != nil {
		return err
	}
	if err := run("ip", "link", "set", vethNS, "netns", ns); err != nil {
		return err
	}
	if err := run("ip", "addr", "add", hostVethIP, "dev", vethHost); err != nil {
		return err
	}
	if err := run("ip", "link", "set", vethHost, "up"); err != nil {
		return err
	}
	nsRun := func(args ...string) error {
		return run(append([]string{"ip", "netns", "exec", ns}, args...)...)
	}
	if err := nsRun("ip", "addr", "add", nsVethIP, "dev", vethNS); err != nil {
		return err
	}
	if err := nsRun("ip", "link", "set", vethNS, "up"); err != nil {
		return err
	}
	if err := nsRun("ip", "addr", "add", ip+"/16", "dev", tunName); err != nil {
		return err
	}
	_ = nsRun("ip", "link", "set", tunName, "mtu", strconv.Itoa(mtu))
	if err := nsRun("ip", "link", "set", tunName, "up"); err != nil {
		return err
	}
	if err := nsRun("ip", "link", "set", "lo", "up"); err != nil {
		return err
	}
	if err := nsRun("ip", "route", "add", "default", "dev", tunName); err != nil {
		return err
	}
	return nil
}

func parseRawConf(conf string) (ip string, mtu int) {
	mtu = 1280
	for _, line := range strings.Split(conf, "\n") {
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, "IP") {
			if parts := strings.SplitN(line, "=", 2); len(parts) == 2 {
				ip = strings.TrimSpace(parts[1])
				if i := strings.IndexByte(ip, '/'); i >= 0 {
					ip = ip[:i]
				}
			}
		}
		if strings.HasPrefix(line, "MTU") {
			if parts := strings.SplitN(line, "=", 2); len(parts) == 2 {
				if n, e := strconv.Atoi(strings.TrimSpace(parts[1])); e == nil {
					mtu = n
				}
			}
		}
	}
	return
}

var (
	bridgeStop chan struct{}
	bridgeDev  tun.Device
	bridgeUpN  atomic.Uint64
	bridgeDnN  atomic.Uint64
)

func startBridge(dev tun.Device, listen string) error {
	stopBridge()
	uc, err := net.DialUDP("udp", nil, &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: mustPort(listen)})
	if err != nil {
		return err
	}
	stop := make(chan struct{})
	bridgeStop = stop
	bridgeDev = dev
	go func() {
		bufs := [][]byte{make([]byte, 2048)}
		sizes := make([]int, 1)
		for {
			select {
			case <-stop:
				return
			default:
			}
			n, err := dev.Read(bufs, sizes, 0)
			if err != nil {
				select {
				case <-stop:
					return
				default:
					time.Sleep(20 * time.Millisecond)
					continue
				}
			}
			for i := 0; i < n; i++ {
				sz := sizes[i]
				if sz < 20 {
					continue
				}
				pkt := bufs[i][:sz]
				if pkt[0]>>4 != 4 {
					continue
				}
				if _, werr := uc.Write(pkt); werr == nil {
					bridgeUpN.Add(1)
				}
			}
		}
	}()
	go func() {
		buf := make([]byte, 2048)
		for {
			_ = uc.SetReadDeadline(time.Now().Add(2 * time.Second))
			nr, err := uc.Read(buf)
			if err != nil {
				select {
				case <-stop:
					return
				default:
					continue
				}
			}
			if nr < 20 || buf[0]>>4 != 4 {
				continue
			}
			pkt := buf[:nr]
			packet := make([]byte, 16+len(pkt))
			copy(packet[16:], pkt)
			if _, werr := dev.Write([][]byte{packet}, 16); werr == nil {
				bridgeDnN.Add(1)
			}
		}
	}()
	go func() {
		t := time.NewTicker(10 * time.Second)
		defer t.Stop()
		for {
			select {
			case <-stop:
				return
			case <-t.C:
				log.Printf("[BRIDGE] up=%d dn=%d", bridgeUpN.Swap(0), bridgeDnN.Swap(0))
			}
		}
	}()
	_, _ = uc.Write([]byte{0x00})
	return nil
}

func stopBridge() {
	if bridgeStop != nil {
		close(bridgeStop)
		bridgeStop = nil
	}
	if bridgeDev != nil {
		_ = bridgeDev.Close()
		bridgeDev = nil
	}
}

// ---------- SOCKS5 server (CONNECT only, no auth) ----------

func socksMain() {
	socksCmd := flag.NewFlagSet("socks", flag.ExitOnError)
	addr := socksCmd.String("addr", "10.255.254.2:1080", "listen addr")
	socksCmd.Parse(os.Args[2:])

	ln, err := net.Listen("tcp", *addr)
	if err != nil {
		log.Fatalf("socks listen: %v", err)
	}
	log.Printf("[SOCKS] listening on %s", *addr)
	fmt.Printf("SOCKS_READY %s\n", *addr)
	for {
		conn, err := ln.Accept()
		if err != nil {
			log.Printf("[SOCKS] accept: %v", err)
			continue
		}
		go handleSocks(conn)
	}
}

func handleSocks(conn net.Conn) {
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(120 * time.Second))
	buf := make([]byte, 512)
	// greeting
	if _, err := io.ReadFull(conn, buf[:2]); err != nil {
		return
	}
	if buf[0] != 5 {
		return
	}
	n := int(buf[1])
	if _, err := io.ReadFull(conn, buf[:n]); err != nil {
		return
	}
	if _, err := conn.Write([]byte{5, 0}); err != nil {
		return
	}
	// request
	if _, err := io.ReadFull(conn, buf[:4]); err != nil {
		return
	}
	if buf[0] != 5 || buf[1] != 1 { // CONNECT only
		_, _ = conn.Write([]byte{5, 7, 0, 1, 0, 0, 0, 0, 0, 0})
		return
	}
	var host string
	switch buf[3] {
	case 1:
		if _, err := io.ReadFull(conn, buf[:4]); err != nil {
			return
		}
		host = net.IP(buf[:4]).String()
	case 3:
		if _, err := io.ReadFull(conn, buf[:1]); err != nil {
			return
		}
		l := int(buf[0])
		if _, err := io.ReadFull(conn, buf[:l]); err != nil {
			return
		}
		host = string(buf[:l])
	case 4:
		if _, err := io.ReadFull(conn, buf[:16]); err != nil {
			return
		}
		host = net.IP(buf[:16]).String()
	default:
		return
	}
	if _, err := io.ReadFull(conn, buf[:2]); err != nil {
		return
	}
	port := binary.BigEndian.Uint16(buf[:2])
	target := net.JoinHostPort(host, strconv.Itoa(int(port)))

	upstream, err := net.DialTimeout("tcp", target, 30*time.Second)
	if err != nil {
		_, _ = conn.Write([]byte{5, 5, 0, 1, 0, 0, 0, 0, 0, 0})
		return
	}
	_ = conn.SetDeadline(time.Time{})
	if _, err := conn.Write([]byte{5, 0, 0, 1, 0, 0, 0, 0, 0, 0}); err != nil {
		_ = upstream.Close()
		return
	}
	go func() {
		_, _ = io.Copy(upstream, conn)
		_ = upstream.Close()
	}()
	_, _ = io.Copy(conn, upstream)
	_ = conn.Close()
}
