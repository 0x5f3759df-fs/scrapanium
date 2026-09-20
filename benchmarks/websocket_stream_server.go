// Independent TLS streaming peer. A tagged corpus is built before the upgrade;
// the timed start message releases it in explicitly selected frame batches.
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
	"strconv"
	"sync"
	"time"
)

type Record struct {
	Frames       int    `json:"data_frames"`
	PayloadBytes int    `json:"data_payload_bytes"`
	FrameBytes   int    `json:"data_frame_bytes"`
	Warmups      int    `json:"warmups"`
	Starts       int    `json:"starts"`
	TLSVersion   uint16 `json:"tls_version"`
	Cipher       uint16 `json:"tls_cipher"`
	Error        string `json:"error,omitempty"`
}

var mu sync.Mutex
var records = map[string]*Record{}

func readBinary(r io.Reader) ([]byte, error) {
	var header [6]byte
	if _, err := io.ReadFull(r, header[:2]); err != nil {
		return nil, err
	}
	if header[0] != 0x82 || header[1]&128 == 0 || header[1]&127 > 125 {
		return nil, fmt.Errorf("invalid control message")
	}
	if _, err := io.ReadFull(r, header[2:]); err != nil {
		return nil, err
	}
	data := make([]byte, int(header[1]&127))
	if _, err := io.ReadFull(r, data); err != nil {
		return nil, err
	}
	for i := range data {
		data[i] ^= header[2+i%4]
	}
	return data, nil
}

func stream(w http.ResponseWriter, r *http.Request) {
	count, _ := strconv.Atoi(r.URL.Query().Get("count"))
	size, _ := strconv.Atoi(r.URL.Query().Get("size"))
	batch, _ := strconv.Atoi(r.URL.Query().Get("batch"))
	if count < 1 || count > 1000000 || size < 8 || size > 65536 || count*size > 64*1024*1024 || (batch != 1 && batch != 64) {
		http.Error(w, "invalid corpus", 400)
		return
	}
	headerSize := 2
	if size >= 126 {
		headerSize = 4
	}
	if size >= 65536 {
		headerSize = 10
	}
	frameSize := headerSize + size
	corpus := make([]byte, count*frameSize)
	for i := 0; i < count; i++ {
		frame := corpus[i*frameSize : (i+1)*frameSize]
		frame[0] = 0x82
		if headerSize == 2 {
			frame[1] = byte(size)
		}
		if headerSize == 4 {
			frame[1] = 126
			binary.BigEndian.PutUint16(frame[2:4], uint16(size))
		}
		if headerSize == 10 {
			frame[1] = 127
			binary.BigEndian.PutUint64(frame[2:10], uint64(size))
		}
		copy(frame[headerSize:], fmt.Sprintf("%08d", i))
		for j := 8; j < size; j++ {
			frame[headerSize+j] = byte((j - 8) * 31 % 128)
		}
	}
	if r.URL.Query().Get("fault") == "corrupt" {
		corpus[headerSize+size-1] ^= 1
	}
	if r.URL.Query().Get("fault") == "swap" && count > 3 {
		temporary := append([]byte(nil), corpus[frameSize:frameSize*2]...)
		copy(corpus[frameSize:frameSize*2], corpus[frameSize*2:frameSize*3])
		copy(corpus[frameSize*2:frameSize*3], temporary)
	}
	record := &Record{TLSVersion: r.TLS.Version, Cipher: r.TLS.CipherSuite}
	mu.Lock()
	records[r.URL.Query().Get("id")] = record
	mu.Unlock()
	conn, rw, err := w.(http.Hijacker).Hijack()
	if err != nil {
		return
	}
	defer conn.Close()
	conn.SetDeadline(time.Now().Add(60 * time.Second))
	digest := sha1.Sum([]byte(r.Header.Get("Sec-WebSocket-Key") + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"))
	fmt.Fprintf(rw, "HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: websocket\r\nSec-WebSocket-Accept: %s\r\n\r\n", base64.StdEncoding.EncodeToString(digest[:]))
	rw.Flush()
	warmup, err := readBinary(rw)
	if err != nil || string(warmup) != "warmup" {
		return
	}
	rw.Write(append([]byte{0x82, 6}, warmup...))
	rw.Flush()
	mu.Lock()
	record.Warmups++
	mu.Unlock()
	start, err := readBinary(rw)
	if err != nil || string(start) != "start" {
		return
	}
	mu.Lock()
	record.Starts++
	mu.Unlock()
	for i := 0; i < count; i += batch {
		end := i + batch
		if end > count {
			end = count
		}
		_, err = conn.Write(corpus[i*frameSize : end*frameSize])
		if err != nil {
			mu.Lock()
			record.Error = err.Error()
			mu.Unlock()
			return
		}
		mu.Lock()
		record.Frames += end - i
		record.PayloadBytes += (end - i) * size
		record.FrameBytes += (end - i) * frameSize
		mu.Unlock()
	}
}

func main() {
	cert := flag.String("cert", "", "certificate path")
	key := flag.String("key", "", "key path")
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
	server := &http.Server{Handler: http.HandlerFunc(stream)}
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
