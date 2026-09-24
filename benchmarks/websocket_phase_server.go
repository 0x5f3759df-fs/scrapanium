// Independent TLS streaming peer for phase diagnostics. The timed frame corpus
// is built before the upgrade; only writes after the start control are measured.
package main

import (
	"bufio"
	"crypto/sha1"
	"crypto/tls"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"strconv"
	"sync"
	"syscall"
	"time"
)

type Record struct {
	Frames                 int    `json:"data_frames"`
	PayloadBytes           int    `json:"data_payload_bytes"`
	FrameBytes             int    `json:"data_frame_bytes"`
	PlaintextBytesWritten  int    `json:"data_plaintext_bytes_written"`
	WriteCalls             int    `json:"send_write_calls"`
	SendIntervalWallNS     int64  `json:"send_interval_wall_ns"`
	TLSWriteWallNSSum      int64  `json:"tls_write_wall_ns_sum"`
	PeerProcessCPUUserNS   int64  `json:"peer_process_cpu_user_ns"`
	PeerProcessCPUSystemNS int64  `json:"peer_process_cpu_system_ns"`
	Completed              bool   `json:"completed"`
	Warmups                int    `json:"warmups"`
	Starts                 int    `json:"starts"`
	TLSVersion             uint16 `json:"tls_version"`
	Cipher                 uint16 `json:"tls_cipher"`
	Error                  string `json:"error,omitempty"`
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

func timevalNS(value syscall.Timeval) int64 {
	return int64(value.Sec)*int64(time.Second) + int64(value.Usec)*int64(time.Microsecond)
}

func processCPU() (userNS, systemNS int64, err error) {
	var usage syscall.Rusage
	if err := syscall.Getrusage(syscall.RUSAGE_SELF, &usage); err != nil {
		return 0, 0, err
	}
	userNS = timevalNS(usage.Utime)
	systemNS = timevalNS(usage.Stime)
	if userNS < 0 || systemNS < 0 {
		return 0, 0, errors.New("negative process CPU counter")
	}
	return userNS, systemNS, nil
}

func setError(id string, err error) {
	if err == nil {
		return
	}
	mu.Lock()
	if record := records[id]; record != nil {
		record.Error = err.Error()
	}
	mu.Unlock()
}

func publishSendRecord(id string, send Record, err error, completed bool) {
	mu.Lock()
	if record := records[id]; record != nil {
		record.Frames = send.Frames
		record.PayloadBytes = send.PayloadBytes
		record.FrameBytes = send.FrameBytes
		record.PlaintextBytesWritten = send.PlaintextBytesWritten
		record.WriteCalls = send.WriteCalls
		record.SendIntervalWallNS = send.SendIntervalWallNS
		record.TLSWriteWallNSSum = send.TLSWriteWallNSSum
		record.PeerProcessCPUUserNS = send.PeerProcessCPUUserNS
		record.PeerProcessCPUSystemNS = send.PeerProcessCPUSystemNS
		if err != nil {
			record.Error = err.Error()
		}
		// Publish completion last, while holding the same lock used by stats
		// snapshots, so readers never see a partially finalized record.
		record.Completed = completed
	}
	mu.Unlock()
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
	id := r.URL.Query().Get("id")
	record := &Record{TLSVersion: r.TLS.Version, Cipher: r.TLS.CipherSuite}
	mu.Lock()
	records[id] = record
	mu.Unlock()
	conn, rw, err := w.(http.Hijacker).Hijack()
	if err != nil {
		setError(id, fmt.Errorf("hijack: %w", err))
		return
	}
	defer conn.Close()
	if err := conn.SetDeadline(time.Now().Add(60 * time.Second)); err != nil {
		setError(id, fmt.Errorf("set connection deadline: %w", err))
		return
	}
	digest := sha1.Sum([]byte(r.Header.Get("Sec-WebSocket-Key") + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"))
	if _, err := fmt.Fprintf(rw, "HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: websocket\r\nSec-WebSocket-Accept: %s\r\n\r\n", base64.StdEncoding.EncodeToString(digest[:])); err != nil {
		setError(id, fmt.Errorf("write upgrade response: %w", err))
		return
	}
	if err := rw.Flush(); err != nil {
		setError(id, fmt.Errorf("flush upgrade response: %w", err))
		return
	}
	warmup, err := readBinary(rw)
	if err != nil || string(warmup) != "warmup" {
		if err == nil {
			err = errors.New("invalid warmup control")
		}
		setError(id, err)
		return
	}
	if _, err := rw.Write(append([]byte{0x82, 6}, warmup...)); err != nil {
		setError(id, fmt.Errorf("write warmup echo: %w", err))
		return
	}
	if err := rw.Flush(); err != nil {
		setError(id, fmt.Errorf("flush warmup echo: %w", err))
		return
	}
	mu.Lock()
	record.Warmups++
	mu.Unlock()
	start, err := readBinary(rw)
	if err != nil || string(start) != "start" {
		if err == nil {
			err = errors.New("invalid start control")
		}
		setError(id, err)
		return
	}
	mu.Lock()
	record.Starts++
	mu.Unlock()

	cpuUserBefore, cpuSystemBefore, err := processCPU()
	if err != nil {
		setError(id, fmt.Errorf("read process CPU before send interval: %w", err))
		return
	}
	intervalStart := time.Now()
	var send Record
	var writeErr error
	for i := 0; i < count; i += batch {
		end := i + batch
		if end > count {
			end = count
		}
		data := corpus[i*frameSize : end*frameSize]
		writeStart := time.Now()
		n, err := conn.Write(data)
		writeDuration := time.Since(writeStart)
		if writeDuration < 0 {
			writeErr = errors.New("negative TLS Write duration")
			break
		}
		send.WriteCalls++
		send.TLSWriteWallNSSum += int64(writeDuration)
		send.PlaintextBytesWritten += n
		if err != nil {
			writeErr = fmt.Errorf("TLS Write: %w", err)
			break
		}
		if n != len(data) {
			writeErr = io.ErrShortWrite
			break
		}
		send.Frames += end - i
		send.PayloadBytes += (end - i) * size
		send.FrameBytes += (end - i) * frameSize
	}
	intervalEnd := time.Now()
	send.SendIntervalWallNS = int64(intervalEnd.Sub(intervalStart))
	if send.SendIntervalWallNS < 0 {
		writeErr = errors.New("negative send interval duration")
	}
	cpuUserAfter, cpuSystemAfter, cpuErr := processCPU()
	if cpuErr != nil {
		if writeErr == nil {
			writeErr = fmt.Errorf("read process CPU after send interval: %w", cpuErr)
		} else {
			writeErr = fmt.Errorf("%v; read process CPU after send interval: %w", writeErr, cpuErr)
		}
		publishSendRecord(id, send, writeErr, false)
		return
	}
	send.PeerProcessCPUUserNS = cpuUserAfter - cpuUserBefore
	send.PeerProcessCPUSystemNS = cpuSystemAfter - cpuSystemBefore
	if send.PeerProcessCPUUserNS < 0 || send.PeerProcessCPUSystemNS < 0 {
		if writeErr == nil {
			writeErr = errors.New("negative process CPU delta")
		}
	}
	completed := true // finalized, including a terminal write error if present
	publishSendRecord(id, send, writeErr, completed)
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
