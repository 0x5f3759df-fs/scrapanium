// Independent TLS peer for the bounded WebSocket attribution diagnostic.
// Historical phase peers and their reports remain frozen; this peer adds a
// labeled Go CPU profile and a raw runtime trace around positive send epochs.
package main

import (
	"bufio"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha1"
	"crypto/sha256"
	"crypto/tls"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"runtime/pprof"
	"runtime/trace"
)

const cpuSamplesPerLabelFloor int64 = 80

type Record struct {
	ID                    string `json:"run_id"`
	Client                string `json:"client"`
	Mode                  string `json:"mode"`
	Batch                 int    `json:"batch"`
	Fault                 string `json:"fault,omitempty"`
	Profiled              bool   `json:"profiled"`
	Frames                int    `json:"data_frames"`
	PayloadBytes          int    `json:"data_payload_bytes"`
	FrameBytes            int    `json:"data_frame_bytes"`
	PlaintextBytesWritten int    `json:"data_plaintext_bytes_written"`
	WriteCalls            int    `json:"send_write_calls"`
	SendIntervalWallNS    int64  `json:"send_interval_wall_ns"`
	TLSWriteWallNSSum     int64  `json:"tls_write_wall_ns_sum"`
	PeerProcessCPUUserNS  int64  `json:"peer_process_cpu_user_ns"`
	PeerProcessCPUSysNS   int64  `json:"peer_process_cpu_system_ns"`
	Completed             bool   `json:"completed"`
	Warmups               int    `json:"warmups"`
	Starts                int    `json:"starts"`
	TLSVersion            uint16 `json:"tls_version"`
	Cipher                uint16 `json:"tls_cipher"`
	Error                 string `json:"error,omitempty"`
}

type ProfileSummary struct {
	Started                 bool             `json:"started"`
	Stopped                 bool             `json:"stopped"`
	ExpectedProfileRuns     int              `json:"expected_profile_runs"`
	ObservedProfileRuns     int              `json:"observed_profile_runs"`
	ProfileWindowWallNS     int64            `json:"profile_window_wall_ns"`
	CPUProfilePath          string           `json:"cpu_profile_path,omitempty"`
	CPUProfileSHA256        string           `json:"cpu_profile_sha256,omitempty"`
	CPUProfileBytes         int64            `json:"cpu_profile_bytes"`
	CPUProfileParseable     bool             `json:"cpu_profile_parseable"`
	CPUProfileSampleCount   int64            `json:"cpu_profile_sample_count"`
	CPULabeledSampleCount   int64            `json:"cpu_labeled_sample_count"`
	CPUUnlabeledSampleCount int64            `json:"cpu_unlabeled_sample_count"`
	CPUSamplesByLabel       map[string]int64 `json:"cpu_samples_by_label,omitempty"`
	CPUSamplesPerLabelFloor int64            `json:"cpu_samples_per_label_floor"`
	CPUSampleFloorMet       bool             `json:"cpu_sample_floor_met"`
	TracePath               string           `json:"trace_path,omitempty"`
	TraceSHA256             string           `json:"trace_sha256,omitempty"`
	TraceBytes              int64            `json:"trace_bytes"`
	TraceParseable          bool             `json:"trace_parseable"`
	TraceParseError         string           `json:"trace_parse_error,omitempty"`
	Errors                  []string         `json:"errors,omitempty"`
}

type PeerState struct {
	mu               sync.Mutex
	records          map[string]*Record
	activeHandlers   int
	profileActive    bool
	profileStopping  bool
	profileStarted   bool
	profileExpected  int
	profileRuns      map[string]bool
	profileStartedAt time.Time
	cpuFile          *os.File
	traceFile        *os.File
	profileSummary   ProfileSummary
	cpuPath          string
	tracePath        string
}

type controlReply struct {
	Command             string            `json:"command"`
	OK                  bool              `json:"ok"`
	Error               string            `json:"error,omitempty"`
	Records             map[string]Record `json:"records,omitempty"`
	Profile             *ProfileSummary   `json:"profile,omitempty"`
	ExpectedProfileRuns int               `json:"expected_profile_runs,omitempty"`
}

func timevalNS(value syscall.Timeval) int64 {
	return int64(value.Sec)*int64(time.Second) + int64(value.Usec)*int64(time.Microsecond)
}

// processCPU deliberately uses RUSAGE_SELF: this is whole-process CPU, not a
// writer-goroutine or OS-thread measurement.
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

func readBinary(r io.Reader) ([]byte, error) {
	var header [6]byte
	if _, err := io.ReadFull(r, header[:2]); err != nil {
		return nil, err
	}
	if header[0] != 0x82 || header[1]&128 == 0 || header[1]&127 > 125 {
		return nil, errors.New("invalid control message")
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

func validRunID(id string) bool {
	if id == "" || len(id) > 128 {
		return false
	}
	for _, r := range id {
		if !(r >= 'a' && r <= 'z') && !(r >= 'A' && r <= 'Z') &&
			!(r >= '0' && r <= '9') && r != '-' && r != '_' && r != '.' {
			return false
		}
	}
	return true
}

func validClient(client string) bool {
	return client == "scrapanium-bend" || client == "curl_cffi-matched"
}

func validMode(mode string) bool { return mode == "control" || mode == "attribution" }

func (s *PeerState) enterHandler() {
	s.mu.Lock()
	s.activeHandlers++
	s.mu.Unlock()
}

func (s *PeerState) leaveHandler() {
	s.mu.Lock()
	s.activeHandlers--
	s.mu.Unlock()
}

func (s *PeerState) putRecord(record *Record) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, exists := s.records[record.ID]; exists {
		return fmt.Errorf("duplicate run_id %q", record.ID)
	}
	s.records[record.ID] = record
	return nil
}

func (s *PeerState) setError(id string, err error) {
	if err == nil {
		return
	}
	s.mu.Lock()
	if record := s.records[id]; record != nil {
		record.Error = err.Error()
	}
	s.mu.Unlock()
}

func (s *PeerState) publishSendRecord(id string, send Record, err error, completed bool) {
	s.mu.Lock()
	if record := s.records[id]; record != nil {
		record.Frames = send.Frames
		record.PayloadBytes = send.PayloadBytes
		record.FrameBytes = send.FrameBytes
		record.PlaintextBytesWritten = send.PlaintextBytesWritten
		record.WriteCalls = send.WriteCalls
		record.SendIntervalWallNS = send.SendIntervalWallNS
		record.TLSWriteWallNSSum = send.TLSWriteWallNSSum
		record.PeerProcessCPUUserNS = send.PeerProcessCPUUserNS
		record.PeerProcessCPUSysNS = send.PeerProcessCPUSysNS
		if err != nil {
			record.Error = err.Error()
		}
		// Completion is published last under the stats lock so snapshots cannot
		// observe a partially finalized record.
		record.Completed = completed
	}
	s.mu.Unlock()
}

func (s *PeerState) beginProfileRun(record *Record) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if record.Fault != "" {
		if s.profileActive || s.profileStopping {
			return errors.New("negative-control request reached the peer during profile window")
		}
		return nil
	}
	if !s.profileActive || s.profileStopping {
		return errors.New("positive request reached the peer outside the profile window")
	}
	if s.profileRuns[record.ID] {
		return fmt.Errorf("duplicate profiled run_id %q", record.ID)
	}
	if len(s.profileRuns) >= s.profileExpected {
		return fmt.Errorf("received more profiled runs than planned (%d)", s.profileExpected)
	}
	record.Profiled = true
	s.profileRuns[record.ID] = true
	return nil
}

func writeFrames(conn net.Conn, corpus []byte, count, size, frameSize, batch int) (Record, error) {
	var send Record
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
			return send, errors.New("negative TLS Write duration")
		}
		send.WriteCalls++
		send.TLSWriteWallNSSum += int64(writeDuration)
		send.PlaintextBytesWritten += n
		if err != nil {
			return send, fmt.Errorf("TLS Write: %w", err)
		}
		if n != len(data) {
			return send, io.ErrShortWrite
		}
		send.Frames += end - i
		send.PayloadBytes += (end - i) * size
		send.FrameBytes += (end - i) * frameSize
	}
	return send, nil
}

func (s *PeerState) stream(w http.ResponseWriter, r *http.Request) {
	count, countErr := strconv.Atoi(r.URL.Query().Get("count"))
	size, sizeErr := strconv.Atoi(r.URL.Query().Get("size"))
	batch, batchErr := strconv.Atoi(r.URL.Query().Get("batch"))
	client := r.URL.Query().Get("client")
	mode := r.URL.Query().Get("mode")
	fault := r.URL.Query().Get("fault")
	id := r.URL.Query().Get("id")
	if countErr != nil || sizeErr != nil || batchErr != nil ||
		(count != 8 && count != 1024) || size != 65536 ||
		(count*size > 64*1024*1024) || (batch != 1 && batch != 64) ||
		!validClient(client) || !validMode(mode) || !validRunID(id) ||
		(fault != "" && fault != "corrupt" && fault != "swap") {
		http.Error(w, "invalid attribution corpus or labels", http.StatusBadRequest)
		return
	}
	if fault != "" && count != 8 {
		http.Error(w, "negative controls must use the eight-message corpus", http.StatusBadRequest)
		return
	}
	if fault != "" && count < 4 {
		http.Error(w, "negative-control corpus is too short", http.StatusBadRequest)
		return
	}
	s.enterHandler()
	defer s.leaveHandler()

	headerSize := 10 // the fixed 65,536-byte payload uses the 64-bit frame length form
	frameSize := headerSize + size
	corpus := make([]byte, count*frameSize)
	for i := 0; i < count; i++ {
		frame := corpus[i*frameSize : (i+1)*frameSize]
		frame[0] = 0x82
		frame[1] = 127
		binary.BigEndian.PutUint64(frame[2:10], uint64(size))
		copy(frame[headerSize:], fmt.Sprintf("%08d", i))
		for j := 8; j < size; j++ {
			frame[headerSize+j] = byte((j - 8) * 31 % 128)
		}
	}
	if fault == "corrupt" {
		corpus[headerSize+size-1] ^= 1
	}
	if fault == "swap" {
		temporary := append([]byte(nil), corpus[frameSize:frameSize*2]...)
		copy(corpus[frameSize:frameSize*2], corpus[frameSize*2:frameSize*3])
		copy(corpus[frameSize*2:frameSize*3], temporary)
	}
	if err := s.putRecord(&Record{ID: id, Client: client, Mode: mode, Batch: batch,
		Fault: fault, TLSVersion: r.TLS.Version, Cipher: r.TLS.CipherSuite}); err != nil {
		http.Error(w, err.Error(), http.StatusConflict)
		return
	}
	conn, rw, err := w.(http.Hijacker).Hijack()
	if err != nil {
		s.setError(id, fmt.Errorf("hijack: %w", err))
		return
	}
	defer conn.Close()
	if err := conn.SetDeadline(time.Now().Add(90 * time.Second)); err != nil {
		s.setError(id, fmt.Errorf("set connection deadline: %w", err))
		return
	}
	digest := sha1.Sum([]byte(r.Header.Get("Sec-WebSocket-Key") + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"))
	if _, err := fmt.Fprintf(rw, "HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: websocket\r\nSec-WebSocket-Accept: %s\r\n\r\n", base64.StdEncoding.EncodeToString(digest[:])); err != nil {
		s.setError(id, fmt.Errorf("write upgrade response: %w", err))
		return
	}
	if err := rw.Flush(); err != nil {
		s.setError(id, fmt.Errorf("flush upgrade response: %w", err))
		return
	}
	warmup, err := readBinary(rw)
	if err != nil || string(warmup) != "warmup" {
		if err == nil {
			err = errors.New("invalid warmup control")
		}
		s.setError(id, err)
		return
	}
	if _, err := rw.Write(append([]byte{0x82, 6}, warmup...)); err != nil {
		s.setError(id, fmt.Errorf("write warmup echo: %w", err))
		return
	}
	if err := rw.Flush(); err != nil {
		s.setError(id, fmt.Errorf("flush warmup echo: %w", err))
		return
	}
	s.mu.Lock()
	if record := s.records[id]; record != nil {
		record.Warmups++
	}
	s.mu.Unlock()
	start, err := readBinary(rw)
	if err != nil || string(start) != "start" {
		if err == nil {
			err = errors.New("invalid start control")
		}
		s.setError(id, err)
		return
	}
	s.mu.Lock()
	if record := s.records[id]; record != nil {
		record.Starts++
	}
	current := *s.records[id]
	s.mu.Unlock()
	if err := s.beginProfileRun(&current); err != nil {
		s.setError(id, err)
		return
	}
	if current.Profiled {
		s.mu.Lock()
		if record := s.records[id]; record != nil {
			record.Profiled = true
		}
		s.mu.Unlock()
	}

	cpuUserBefore, cpuSystemBefore, err := processCPU()
	if err != nil {
		s.publishSendRecord(id, Record{}, fmt.Errorf("read process CPU before send interval: %w", err), false)
		return
	}
	intervalStart := time.Now()
	var send Record
	var writeErr error
	if current.Profiled {
		labels := pprof.Labels("client", current.Client, "mode", current.Mode,
			"batch", strconv.Itoa(current.Batch))
		pprof.Do(context.Background(), labels, func(ctx context.Context) {
			taskCtx, task := trace.NewTask(ctx, "websocket-send")
			defer task.End()
			trace.Log(taskCtx, "run_id", current.ID)
			trace.Log(taskCtx, "client", current.Client)
			trace.Log(taskCtx, "mode", current.Mode)
			trace.Log(taskCtx, "batch", strconv.Itoa(current.Batch))
			trace.WithRegion(taskCtx, "websocket-send-loop", func() {
				send, writeErr = writeFrames(conn, corpus, count, size, frameSize, batch)
			})
		})
	} else {
		send, writeErr = writeFrames(conn, corpus, count, size, frameSize, batch)
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
		s.publishSendRecord(id, send, writeErr, false)
		return
	}
	send.PeerProcessCPUUserNS = cpuUserAfter - cpuUserBefore
	send.PeerProcessCPUSysNS = cpuSystemAfter - cpuSystemBefore
	if send.PeerProcessCPUUserNS < 0 || send.PeerProcessCPUSysNS < 0 {
		if writeErr == nil {
			writeErr = errors.New("negative process CPU delta")
		}
	}
	// A write error still publishes finalized partial counters with Error set;
	// Completed means finalized, not that the intended corpus fully succeeded.
	s.publishSendRecord(id, send, writeErr, true)
}

func openExclusive(path string) (*os.File, error) {
	if path == "" || !filepath.IsAbs(path) {
		return nil, fmt.Errorf("profile output path must be absolute: %q", path)
	}
	return os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
}

func (s *PeerState) startProfile(expected int) error {
	if expected < 1 || expected > 10000 {
		return fmt.Errorf("invalid expected profile run count %d", expected)
	}
	s.mu.Lock()
	if s.profileStarted || s.profileActive || s.profileStopping {
		s.mu.Unlock()
		return errors.New("profile window can only be started once")
	}
	if s.activeHandlers != 0 {
		s.mu.Unlock()
		return fmt.Errorf("cannot start profile with %d active request handlers", s.activeHandlers)
	}
	s.profileStarted = true
	s.profileExpected = expected
	s.profileRuns = make(map[string]bool, expected)
	s.mu.Unlock()

	cpuFile, err := openExclusive(s.cpuPath)
	if err != nil {
		return fmt.Errorf("create CPU profile: %w", err)
	}
	traceFile, err := openExclusive(s.tracePath)
	if err != nil {
		_ = cpuFile.Close()
		return fmt.Errorf("create runtime trace: %w", err)
	}
	if err := pprof.StartCPUProfile(cpuFile); err != nil {
		_ = cpuFile.Close()
		_ = traceFile.Close()
		return fmt.Errorf("start CPU profile: %w", err)
	}
	if err := trace.Start(traceFile); err != nil {
		pprof.StopCPUProfile()
		_ = cpuFile.Close()
		_ = traceFile.Close()
		return fmt.Errorf("start runtime trace: %w", err)
	}
	s.mu.Lock()
	s.cpuFile = cpuFile
	s.traceFile = traceFile
	s.profileStartedAt = time.Now()
	s.profileActive = true
	s.profileSummary = ProfileSummary{
		Started:                 true,
		ExpectedProfileRuns:     expected,
		CPUSamplesPerLabelFloor: cpuSamplesPerLabelFloor,
		CPUProfilePath:          s.cpuPath,
		TracePath:               s.tracePath,
	}
	s.mu.Unlock()
	return nil
}

func (s *PeerState) stopProfile() (ProfileSummary, error) {
	s.mu.Lock()
	if !s.profileActive {
		summary := s.profileSummary
		s.mu.Unlock()
		return summary, errors.New("profile window is not active")
	}
	if s.activeHandlers != 0 {
		active := s.activeHandlers
		s.mu.Unlock()
		return ProfileSummary{}, fmt.Errorf("cannot stop profile with %d active request handlers", active)
	}
	s.profileActive = false
	s.profileStopping = true
	observed := len(s.profileRuns)
	expected := s.profileExpected
	startedAt := s.profileStartedAt
	cpuFile := s.cpuFile
	traceFile := s.traceFile
	s.mu.Unlock()

	var validationErrors []string
	if observed != expected {
		validationErrors = append(validationErrors, fmt.Sprintf("profiled %d runs, expected %d", observed, expected))
	}
	s.mu.Lock()
	for id := range s.profileRuns {
		record := s.records[id]
		if record == nil || !record.Completed || record.Error != "" {
			validationErrors = append(validationErrors, fmt.Sprintf("profiled run %s lacks a successful finalized peer record", id))
		}
	}
	s.mu.Unlock()

	trace.Stop()
	pprof.StopCPUProfile()
	if cpuFile != nil {
		if err := cpuFile.Sync(); err != nil {
			validationErrors = append(validationErrors, "sync CPU profile: "+err.Error())
		}
		if err := cpuFile.Close(); err != nil {
			validationErrors = append(validationErrors, "close CPU profile: "+err.Error())
		}
	}
	if traceFile != nil {
		if err := traceFile.Sync(); err != nil {
			validationErrors = append(validationErrors, "sync runtime trace: "+err.Error())
		}
		if err := traceFile.Close(); err != nil {
			validationErrors = append(validationErrors, "close runtime trace: "+err.Error())
		}
	}

	summary := ProfileSummary{
		Started:                 true,
		Stopped:                 true,
		ExpectedProfileRuns:     expected,
		ObservedProfileRuns:     observed,
		ProfileWindowWallNS:     int64(time.Since(startedAt)),
		CPUProfilePath:          s.cpuPath,
		TracePath:               s.tracePath,
		CPUSamplesPerLabelFloor: cpuSamplesPerLabelFloor,
	}
	if summary.ProfileWindowWallNS < 0 {
		validationErrors = append(validationErrors, "negative profile window duration")
	}
	if err := populateFileSummary(s.cpuPath, &summary.CPUProfileBytes, &summary.CPUProfileSHA256); err != nil {
		validationErrors = append(validationErrors, "read CPU profile artifact: "+err.Error())
	} else {
		counts, total, unlabeled, parseErr := parseCPUProfile(s.cpuPath)
		if parseErr != nil {
			validationErrors = append(validationErrors, "parse CPU profile: "+parseErr.Error())
		} else {
			summary.CPUProfileParseable = true
			summary.CPUProfileSampleCount = total
			summary.CPUSamplesByLabel = counts
			summary.CPULabeledSampleCount = total - unlabeled
			summary.CPUUnlabeledSampleCount = unlabeled
			summary.CPUSampleFloorMet = len(counts) == 8
			for _, client := range []string{"scrapanium-bend", "curl_cffi-matched"} {
				for _, mode := range []string{"control", "attribution"} {
					for _, batch := range []string{"1", "64"} {
						key := labelKey(client, mode, batch)
						if counts[key] < cpuSamplesPerLabelFloor {
							summary.CPUSampleFloorMet = false
						}
					}
				}
			}
		}
	}
	if err := populateFileSummary(s.tracePath, &summary.TraceBytes, &summary.TraceSHA256); err != nil {
		validationErrors = append(validationErrors, "read runtime trace artifact: "+err.Error())
	} else {
		parseable, parseErr := validateTrace(s.tracePath)
		summary.TraceParseable = parseable
		if parseErr != nil {
			summary.TraceParseError = parseErr.Error()
			validationErrors = append(validationErrors, "parse runtime trace: "+parseErr.Error())
		}
	}
	summary.Errors = validationErrors

	s.mu.Lock()
	s.profileStopping = false
	s.profileSummary = summary
	s.mu.Unlock()
	if len(validationErrors) > 0 {
		return summary, errors.New(strings.Join(validationErrors, "; "))
	}
	return summary, nil
}

func labelKey(client, mode, batch string) string {
	return client + "|" + mode + "|" + batch
}

func populateFileSummary(path string, size *int64, digest *string) error {
	file, err := os.Open(path)
	if err != nil {
		return err
	}
	defer file.Close()
	stat, err := file.Stat()
	if err != nil {
		return err
	}
	if stat.Size() <= 0 {
		return errors.New("profile artifact is empty")
	}
	h := sha256.New()
	n, err := io.Copy(h, file)
	if err != nil {
		return err
	}
	if n != stat.Size() {
		return fmt.Errorf("read %d bytes but stat reported %d", n, stat.Size())
	}
	*size = n
	*digest = hex.EncodeToString(h.Sum(nil))
	return nil
}

func validateTrace(path string) (bool, error) {
	goPath, err := exec.LookPath("go")
	if err != nil {
		return false, fmt.Errorf("locate Go tool for trace validation: %w", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()
	command := exec.CommandContext(ctx, goPath, "tool", "trace", "-d=parsed", path)
	command.Stdout = io.Discard
	var stderr bytes.Buffer
	command.Stderr = &stderr
	if err := command.Run(); err != nil {
		return false, fmt.Errorf("go tool trace validation failed: %w: %s", err, strings.TrimSpace(stderr.String()))
	}
	return true, nil
}

// The Go runtime writes pprof's standard gzip/protobuf Profile message. This
// small reader counts the first (samples/count) value by the three labels set
// only inside the send-loop pprof.Do region; it does not symbolize stacks.
func parseCPUProfile(path string) (map[string]int64, int64, int64, error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, 0, 0, err
	}
	defer file.Close()
	zr, err := gzip.NewReader(file)
	if err != nil {
		return nil, 0, 0, err
	}
	defer zr.Close()
	const maxProfileBytes = 256 << 20
	data, err := io.ReadAll(io.LimitReader(zr, maxProfileBytes+1))
	if err != nil {
		return nil, 0, 0, err
	}
	if len(data) == maxProfileBytes+1 {
		return nil, 0, 0, errors.New("decompressed CPU profile exceeds 256 MiB limit")
	}
	var stringsTable []string
	var sampleTypes [][]byte
	var samples [][]byte
	err = visitProtoFields(data, func(field int, wire int, value uint64, payload []byte) error {
		if wire != 2 {
			return nil
		}
		switch field {
		case 1:
			sampleTypes = append(sampleTypes, payload)
		case 2:
			samples = append(samples, payload)
		case 6:
			stringsTable = append(stringsTable, string(payload))
		}
		return nil
	})
	if err != nil {
		return nil, 0, 0, fmt.Errorf("decode profile message: %w", err)
	}
	if len(stringsTable) == 0 || stringsTable[0] != "" {
		return nil, 0, 0, errors.New("profile string table is missing required empty entry")
	}
	if len(sampleTypes) == 0 {
		return nil, 0, 0, errors.New("profile contains no sample type")
	}
	typeName, unitName, err := parseValueType(sampleTypes[0], stringsTable)
	if err != nil {
		return nil, 0, 0, err
	}
	if typeName != "samples" || unitName != "count" {
		return nil, 0, 0, fmt.Errorf("first CPU sample type is %q/%q, expected samples/count", typeName, unitName)
	}
	counts := make(map[string]int64)
	var total, labeled int64
	for _, rawSample := range samples {
		values, labels, err := parseProfileSample(rawSample, stringsTable)
		if err != nil {
			return nil, 0, 0, err
		}
		if len(values) != len(sampleTypes) {
			return nil, 0, 0, fmt.Errorf("CPU profile sample has %d values, expected %d", len(values), len(sampleTypes))
		}
		if values[0] > uint64(^uint64(0)>>1) {
			return nil, 0, 0, errors.New("CPU profile sample count overflows int64")
		}
		count := int64(values[0])
		if count < 0 || total > int64(^uint64(0)>>1)-count {
			return nil, 0, 0, errors.New("invalid or overflowing CPU profile sample count")
		}
		total += count
		if len(labels) == 0 {
			continue
		}
		client, clientOK := labels["client"]
		mode, modeOK := labels["mode"]
		batch, batchOK := labels["batch"]
		if !(clientOK && modeOK && batchOK) || len(labels) != 3 ||
			!validClient(client) || !validMode(mode) || (batch != "1" && batch != "64") {
			return nil, 0, 0, fmt.Errorf("unexpected partial or invalid CPU sample labels: %#v", labels)
		}
		key := labelKey(client, mode, batch)
		if counts[key] > int64(^uint64(0)>>1)-count || labeled > int64(^uint64(0)>>1)-count {
			return nil, 0, 0, errors.New("CPU profile label sample count overflow")
		}
		counts[key] += count
		labeled += count
	}
	if total < labeled {
		return nil, 0, 0, errors.New("labeled CPU sample count exceeds total")
	}
	return counts, total, total - labeled, nil
}

func parseValueType(raw []byte, stringsTable []string) (string, string, error) {
	var typeIndex, unitIndex uint64
	err := visitProtoFields(raw, func(field int, wire int, value uint64, payload []byte) error {
		if wire != 0 {
			return nil
		}
		switch field {
		case 1:
			typeIndex = value
		case 2:
			unitIndex = value
		}
		return nil
	})
	if err != nil {
		return "", "", err
	}
	typeName, err := stringAt(stringsTable, typeIndex)
	if err != nil {
		return "", "", err
	}
	unitName, err := stringAt(stringsTable, unitIndex)
	if err != nil {
		return "", "", err
	}
	return typeName, unitName, nil
}

func parseProfileSample(raw []byte, stringsTable []string) ([]uint64, map[string]string, error) {
	var values []uint64
	labels := make(map[string]string)
	err := visitProtoFields(raw, func(field int, wire int, value uint64, payload []byte) error {
		if field == 2 {
			switch wire {
			case 0:
				values = append(values, value)
			case 2: // packed repeated int64
				position := 0
				for position < len(payload) {
					packed, err := readProtoVarint(payload, &position)
					if err != nil {
						return err
					}
					values = append(values, packed)
				}
			default:
				return fmt.Errorf("unexpected wire type %d for profile sample value", wire)
			}
			return nil
		}
		if field == 3 {
			if wire != 2 {
				return fmt.Errorf("unexpected wire type %d for profile sample label", wire)
			}
			keyIndex, valueIndex, err := parseStringLabel(payload)
			if err != nil {
				return err
			}
			key, err := stringAt(stringsTable, keyIndex)
			if err != nil {
				return err
			}
			valueText, err := stringAt(stringsTable, valueIndex)
			if err != nil {
				return err
			}
			if _, duplicate := labels[key]; duplicate {
				return fmt.Errorf("duplicate CPU sample label %q", key)
			}
			labels[key] = valueText
		}
		return nil
	})
	return values, labels, err
}

func parseStringLabel(raw []byte) (uint64, uint64, error) {
	var keyIndex, valueIndex uint64
	var hasKey, hasValue bool
	err := visitProtoFields(raw, func(field int, wire int, value uint64, payload []byte) error {
		if wire != 0 {
			return nil
		}
		switch field {
		case 1:
			keyIndex, hasKey = value, true
		case 2:
			valueIndex, hasValue = value, true
		case 3:
			return errors.New("numeric pprof labels are unsupported in this diagnostic")
		}
		return nil
	})
	if err != nil {
		return 0, 0, err
	}
	if !hasKey || !hasValue {
		return 0, 0, errors.New("CPU profile label omitted key or string value")
	}
	return keyIndex, valueIndex, nil
}

func stringAt(table []string, index uint64) (string, error) {
	if index >= uint64(len(table)) {
		return "", fmt.Errorf("profile string-table index %d is out of range", index)
	}
	return table[index], nil
}

func visitProtoFields(data []byte, visit func(field int, wire int, value uint64, payload []byte) error) error {
	position := 0
	for position < len(data) {
		tag, err := readProtoVarint(data, &position)
		if err != nil {
			return err
		}
		field, wire := int(tag>>3), int(tag&7)
		if field <= 0 {
			return errors.New("protobuf field number must be positive")
		}
		var value uint64
		var payload []byte
		switch wire {
		case 0:
			value, err = readProtoVarint(data, &position)
		case 1:
			if len(data)-position < 8 {
				return io.ErrUnexpectedEOF
			}
			position += 8
		case 2:
			var length uint64
			length, err = readProtoVarint(data, &position)
			if err == nil {
				if length > uint64(len(data)-position) {
					err = io.ErrUnexpectedEOF
				} else {
					payload = data[position : position+int(length)]
					position += int(length)
				}
			}
		case 5:
			if len(data)-position < 4 {
				return io.ErrUnexpectedEOF
			}
			position += 4
		default:
			return fmt.Errorf("unsupported protobuf wire type %d", wire)
		}
		if err != nil {
			return err
		}
		if err := visit(field, wire, value, payload); err != nil {
			return err
		}
	}
	return nil
}

func readProtoVarint(data []byte, position *int) (uint64, error) {
	var value uint64
	for shift := uint(0); shift < 64; shift += 7 {
		if *position >= len(data) {
			return 0, io.ErrUnexpectedEOF
		}
		b := data[*position]
		*position++
		if shift == 63 && b > 1 {
			return 0, errors.New("protobuf varint overflows uint64")
		}
		value |= uint64(b&0x7f) << shift
		if b < 0x80 {
			return value, nil
		}
	}
	return 0, errors.New("protobuf varint is too long")
}

func (s *PeerState) snapshot() map[string]Record {
	s.mu.Lock()
	defer s.mu.Unlock()
	copyRecords := make(map[string]Record, len(s.records))
	for id, record := range s.records {
		copyRecords[id] = *record
	}
	return copyRecords
}

func (s *PeerState) snapshotProfile() ProfileSummary {
	s.mu.Lock()
	defer s.mu.Unlock()
	result := s.profileSummary
	if result.CPUSamplesByLabel != nil {
		result.CPUSamplesByLabel = make(map[string]int64, len(s.profileSummary.CPUSamplesByLabel))
		for key, count := range s.profileSummary.CPUSamplesByLabel {
			result.CPUSamplesByLabel[key] = count
		}
	}
	if result.Errors != nil {
		result.Errors = append([]string(nil), result.Errors...)
	}
	if s.profileActive {
		result.Started = true
		result.ExpectedProfileRuns = s.profileExpected
		result.ObservedProfileRuns = len(s.profileRuns)
		result.CPUProfilePath = s.cpuPath
		result.TracePath = s.tracePath
	}
	return result
}

func emit(reply controlReply) error {
	encoded, err := json.Marshal(reply)
	if err != nil {
		return err
	}
	_, err = fmt.Println(string(encoded))
	return err
}

func main() {
	certPath := flag.String("cert", "", "TLS certificate path")
	keyPath := flag.String("key", "", "TLS private key path")
	cpuPath := flag.String("cpu-profile", "", "fresh absolute CPU-profile output path")
	tracePath := flag.String("trace", "", "fresh absolute runtime-trace output path")
	flag.Parse()
	if *cpuPath == "" || *tracePath == "" || *cpuPath == *tracePath {
		fmt.Fprintln(os.Stderr, "-cpu-profile and -trace must be distinct nonempty paths")
		os.Exit(2)
	}
	pair, err := tls.LoadX509KeyPair(*certPath, *keyPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, "load TLS key pair:", err)
		os.Exit(2)
	}
	raw, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		fmt.Fprintln(os.Stderr, "listen:", err)
		os.Exit(2)
	}
	state := &PeerState{
		records:     make(map[string]*Record),
		profileRuns: make(map[string]bool),
		cpuPath:     *cpuPath,
		tracePath:   *tracePath,
	}
	listener := tls.NewListener(raw, &tls.Config{
		Certificates: []tls.Certificate{pair},
		NextProtos:   []string{"http/1.1"},
		MinVersion:   tls.VersionTLS12,
	})
	server := &http.Server{
		Handler:           http.HandlerFunc(state.stream),
		ReadHeaderTimeout: 10 * time.Second,
	}
	serveDone := make(chan error, 1)
	go func() { serveDone <- server.Serve(listener) }()
	fmt.Println("wss://" + raw.Addr().String())
	scanner := bufio.NewScanner(os.Stdin)
	for scanner.Scan() {
		parts := strings.Fields(scanner.Text())
		if len(parts) == 0 {
			_ = emit(controlReply{Command: "", Error: "empty command"})
			continue
		}
		switch parts[0] {
		case "stats":
			_ = emit(controlReply{Command: "stats", OK: true, Records: state.snapshot(), Profile: ptrProfile(state.snapshotProfile())})
		case "profile-start":
			if len(parts) != 2 {
				_ = emit(controlReply{Command: "profile-start", Error: "usage: profile-start EXPECTED_RUNS"})
				continue
			}
			expected, parseErr := strconv.Atoi(parts[1])
			if parseErr != nil {
				_ = emit(controlReply{Command: "profile-start", Error: "expected run count is not an integer"})
				continue
			}
			if err := state.startProfile(expected); err != nil {
				_ = emit(controlReply{Command: "profile-start", Error: err.Error()})
				continue
			}
			_ = emit(controlReply{Command: "profile-start", OK: true, ExpectedProfileRuns: expected})
		case "profile-stop":
			if len(parts) != 1 {
				_ = emit(controlReply{Command: "profile-stop", Error: "usage: profile-stop"})
				continue
			}
			summary, err := state.stopProfile()
			reply := controlReply{Command: "profile-stop", OK: err == nil, Profile: &summary}
			if err != nil {
				reply.Error = err.Error()
			}
			_ = emit(reply)
		case "shutdown":
			if state.snapshotProfile().Started && !state.snapshotProfile().Stopped {
				_ = emit(controlReply{Command: "shutdown", Error: "profile window must be stopped before shutdown"})
				continue
			}
			closeErr := server.Close()
			if closeErr != nil && !errors.Is(closeErr, http.ErrServerClosed) {
				_ = emit(controlReply{Command: "shutdown", Error: closeErr.Error()})
				continue
			}
			_ = emit(controlReply{Command: "shutdown", OK: true})
			<-serveDone
			return
		default:
			_ = emit(controlReply{Command: parts[0], Error: "unknown command"})
		}
	}
	if err := scanner.Err(); err != nil {
		fmt.Fprintln(os.Stderr, "read control protocol:", err)
	}
	if state.snapshotProfile().Started && !state.snapshotProfile().Stopped {
		_, _ = state.stopProfile()
	}
	_ = server.Close()
	<-serveDone
}

func ptrProfile(profile ProfileSummary) *ProfileSummary { return &profile }

// Helpers above implement only the subset of pprof's standard wire format
// needed for CPU sample counts and string labels.
