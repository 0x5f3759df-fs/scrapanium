package main

import (
	"crypto/x509"
	"fmt"
	"io"
	"os"
	"strconv"
	"sync"
	"time"

	http "github.com/bogdanfinn/fhttp"
	client "github.com/bogdanfinn/tls-client"
	"github.com/bogdanfinn/tls-client/profiles"
)

func main() {
	if len(os.Args) != 5 {
		panic("url mode count ca")
	}
	url, mode, ca := os.Args[1], os.Args[2], os.Args[4]
	count, err := strconv.Atoi(os.Args[3])
	if err != nil {
		panic(err)
	}
	transport := &client.TransportOptions{MaxIdleConns: 16, MaxIdleConnsPerHost: 16, MaxConnsPerHost: 16}
	if ca != "" {
		data, err := os.ReadFile(ca)
		if err != nil {
			panic(err)
		}
		transport.RootCAs = x509.NewCertPool()
		if !transport.RootCAs.AppendCertsFromPEM(data) {
			panic("invalid CA")
		}
	}
	profile := profiles.Chrome_146
	if selected := os.Getenv("SCRAPANIUM_PROFILE"); selected != "" {
		var ok bool
		profile, ok = profiles.MappedTLSClients[selected]
		if !ok { panic("unknown profile") }
	}
	session, err := client.NewHttpClient(client.NewNoopLogger(), client.WithClientProfile(profile),
		client.WithRandomTLSExtensionOrder(), client.WithTimeoutSeconds(30), client.WithTransportOptions(transport),
		client.WithCookieJar(client.NewCookieJar()))
	if err != nil {
		panic(err)
	}
	defer session.CloseIdleConnections()
	send := func() []byte {
		req, err := http.NewRequest(http.MethodGet, url, nil)
		if err != nil {
			panic(err)
		}
		req.Header = http.Header{"user-agent": {"scrapanium-benchmark"}, "accept": {"*/*"},
			"accept-encoding":   {"gzip, deflate, br, zstd"},
			http.HeaderOrderKey: {"user-agent", "accept", "accept-encoding"}}
		response, err := session.Do(req)
		if err != nil {
			panic(err)
		}
		body, err := io.ReadAll(response.Body)
		response.Body.Close()
		if err != nil || response.StatusCode != 200 {
			panic(fmt.Sprintf("request: %v/%d", err, response.StatusCode))
		}
		return body
	}
	send()
	start := time.Now()
	if mode == "batch" {
		sem := make(chan struct{}, 16)
		var wg sync.WaitGroup
		// Retain every response until batch completion, matching the other clients.
		bodies := make([][]byte, count)
		for i := 0; i < count; i++ {
			wg.Add(1)
			go func(i int) { defer wg.Done(); sem <- struct{}{}; bodies[i] = send(); <-sem }(i)
		}
		wg.Wait()
		for _, body := range bodies {
			if body == nil {
				panic("missing response")
			}
		}
	} else {
		for i := 0; i < count; i++ {
			send()
		}
	}
	fmt.Printf("%.6f\n", float64(time.Since(start).Nanoseconds())/1e6)
}
