// Independent standard-library TLS/RFC6455 echo peer for local benchmarks.
// No compression or batching. Rejects non-final/unmasked/non-binary frames.
package main

import (
	"bufio"
	"crypto/sha1"
	"crypto/tls"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"sync"
	"time"
)

type Record struct {
	Frames           uint64 `json:"frames"`
	PayloadBytes     uint64 `json:"payload_bytes"`
	ClientFrameBytes uint64 `json:"client_frame_bytes"`
	ServerFrameBytes uint64 `json:"server_frame_bytes"`
	TLSVersion       uint16 `json:"tls_version"`
	Cipher           uint16 `json:"tls_cipher"`
	Error            string `json:"error,omitempty"`
}

var mu sync.Mutex
var records = map[string]*Record{}

func echo(w http.ResponseWriter, r *http.Request) {
	record := &Record{TLSVersion: r.TLS.Version, Cipher: r.TLS.CipherSuite}
	id := r.URL.Query().Get("id")
	mu.Lock()
	records[id] = record
	mu.Unlock()
	conn, rw, err := w.(http.Hijacker).Hijack()
	if err != nil {
		return
	}
	defer conn.Close()
	digest := sha1.Sum([]byte(r.Header.Get("Sec-WebSocket-Key") + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"))
	fmt.Fprintf(rw, "HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: websocket\r\nSec-WebSocket-Accept: %s\r\n\r\n", base64.StdEncoding.EncodeToString(digest[:]))
	rw.Flush()
	var header [14]byte
	var data []byte
	for {
		conn.SetDeadline(time.Now().Add(30 * time.Second))
		if _, err = io.ReadFull(rw, header[:2]); err != nil {
			return
		}
		if header[0] != 0x82 || header[1]&128 == 0 {
			mu.Lock()
			record.Error = "expected final masked binary frame"
			mu.Unlock()
			return
		}
		n, lengthBytes := uint64(header[1]&127), 0
		if n == 126 {
			lengthBytes = 2
		} else if n == 127 {
			lengthBytes = 8
		}
		if _, err = io.ReadFull(rw, header[2:2+lengthBytes+4]); err != nil {
			return
		}
		if lengthBytes == 2 {
			n = uint64(binary.BigEndian.Uint16(header[2:4]))
		}
		if lengthBytes == 8 {
			n = binary.BigEndian.Uint64(header[2:10])
		}
		if n > 1048576 {
			return
		}
		if cap(data) < int(n) {
			data = make([]byte, n)
		} else {
			data = data[:n]
		}
		if _, err = io.ReadFull(rw, data); err != nil {
			return
		}
		mask := header[2+lengthBytes : 6+lengthBytes]
		for i := range data {
			data[i] ^= mask[i%4]
		}
		header[1] &= 127
		if _, err = rw.Write(header[:2+lengthBytes]); err != nil {
			return
		}
		if _, err = rw.Write(data); err != nil {
			return
		}
		if err = rw.Flush(); err != nil {
			return
		}
		mu.Lock()
		record.Frames++
		record.PayloadBytes += n
		record.ClientFrameBytes += n + uint64(6+lengthBytes)
		record.ServerFrameBytes += n + uint64(2+lengthBytes)
		mu.Unlock()
	}
}

func main() {
	cert := flag.String("cert", "", "certificate path")
	key := flag.String("key", "", "private key path")
	flag.Parse()
	pair, err := tls.LoadX509KeyPair(*cert, *key)
	if err != nil {
		panic(err)
	}
	raw, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		panic(err)
	}
	listener := tls.NewListener(raw, &tls.Config{Certificates: []tls.Certificate{pair}, NextProtos: []string{"http/1.1"}, MinVersion: tls.VersionTLS12})
	server := &http.Server{Handler: http.HandlerFunc(echo)}
	go server.Serve(listener)
	fmt.Println("wss://" + raw.Addr().String())
	scanner := bufio.NewScanner(os.Stdin)
	for scanner.Scan() {
		mu.Lock()
		output, _ := json.Marshal(records)
		mu.Unlock()
		fmt.Println(string(output))
	}
	server.Close()
}
