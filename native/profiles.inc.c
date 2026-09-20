/* Source-derived profile data. Provenance and limitations: profiles/catalog.json.
 * tls-client data carries its upstream BSD-style license; see THIRD_PARTY.md. */
static const char *sp_chrome152_headers[] = {
      "sec-ch-ua: \"Not;A=Brand\";v=\"8\", \"Chromium\";v=\"152\", \"Google Chrome\";v=\"152\"",
      "sec-ch-ua-mobile: ?0",
      "sec-ch-ua-platform: \"macOS\"",
      "Upgrade-Insecure-Requests: 1",
      "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
      "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
      "Sec-Fetch-Site: none",
      "Sec-Fetch-Mode: navigate",
      "Sec-Fetch-User: ?1",
      "Sec-Fetch-Dest: document",
      "Accept-Encoding: gzip, deflate, br, zstd",
      "Accept-Language: en-US,en;q=0.9",
      "Priority: u=0, i"
};
static const char *sp_firefox148_headers[] = {
      "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:148.0) Gecko/20100101 Firefox/148.0",
      "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
      "Accept-Language: en-US,en;q=0.9",
      "Accept-Encoding: gzip, deflate, br, zstd",
      "Upgrade-Insecure-Requests: 1",
      "Sec-Fetch-Dest: document",
      "Sec-Fetch-Mode: navigate",
      "Sec-Fetch-Site: none",
      "Sec-Fetch-User: ?1",
      "Priority: u=0, i",
      "Te: trailers"
};
static const char sp_chrome152_anchors[] = "44947.2.1,52580.200109.1.12,52580.200109.1.7,11129.9.12,52580.200109.1.10,11129.9.11,52580.200109.1.13,44947.2.14,52580.200109.1.11,11129.9.5,44947.2.13,44947.2.20,11129.9.4,11129.9.8,11129.9.13,11129.9.10,11129.9.7,52580.200109.1.18,11129.9.1,44947.2.6,52580.200109.1.8,44947.2.18,52580.200109.1.19,11129.9.15,44947.2.19,52580.200109.1.9,44947.2.15,11129.9.6";
static const char sp_firefox148_ciphers[] =
  "TLS_AES_128_GCM_SHA256:TLS_CHACHA20_POLY1305_SHA256:TLS_AES_256_GCM_SHA384:"
  "TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256:TLS_ECDHE_RSA_WITH_AES_128_GCM_SHA256:"
  "TLS_ECDHE_ECDSA_WITH_CHACHA20_POLY1305_SHA256:TLS_ECDHE_RSA_WITH_CHACHA20_POLY1305_SHA256:"
  "TLS_ECDHE_ECDSA_WITH_AES_256_GCM_SHA384:TLS_ECDHE_RSA_WITH_AES_256_GCM_SHA384:"
  "TLS_ECDHE_ECDSA_WITH_AES_256_CBC_SHA:TLS_ECDHE_ECDSA_WITH_AES_128_CBC_SHA:"
  "TLS_ECDHE_RSA_WITH_AES_128_CBC_SHA:TLS_ECDHE_RSA_WITH_AES_256_CBC_SHA:"
  "TLS_DHE_RSA_WITH_AES_128_CBC_SHA:TLS_DHE_RSA_WITH_AES_256_CBC_SHA:"
  "TLS_RSA_WITH_AES_128_GCM_SHA256:TLS_RSA_WITH_AES_256_GCM_SHA384:TLS_RSA_WITH_AES_128_CBC_SHA:TLS_RSA_WITH_AES_256_CBC_SHA";
static const char *sp_profile_ids[] = {
  "chrome100",
  "chrome101",
  "chrome104",
  "chrome107",
  "chrome110",
  "chrome116",
  "chrome119",
  "chrome120",
  "chrome123",
  "chrome124",
  "chrome131",
  "chrome131_android",
  "chrome133a",
  "chrome136",
  "chrome142",
  "chrome145",
  "chrome146",
  "chrome150",
  "chrome99",
  "chrome99_android",
  "edge101",
  "edge99",
  "firefox133",
  "firefox135",
  "firefox144",
  "firefox147",
  "okhttp4_android",
  "safari153",
  "safari155",
  "safari170",
  "safari172_ios",
  "safari180",
  "safari180_ios",
  "safari184",
  "safari184_ios",
  "safari260",
  "safari2601",
  "safari260_ios",
  "tor145",
  "firefox148",
  "chrome152_preview",
};
size_t sp_profile_count(void) { return sizeof sp_profile_ids / sizeof *sp_profile_ids; }
const char *sp_profile_name(size_t index) { return index < sp_profile_count() ? sp_profile_ids[index] : NULL; }
static CURLcode sp_profile_headers(CURL *easy, const char *const *values, size_t count) {
  struct curl_slist *headers = NULL;
  for (size_t i = 0; i < count; i++) {
    struct curl_slist *next = curl_slist_append(headers, values[i]);
    if (!next) { curl_slist_free_all(headers); return CURLE_OUT_OF_MEMORY; }
    headers = next;
  }
  CURLcode code = curl_easy_setopt(easy, CURLOPT_HTTPBASEHEADER, headers);
  curl_slist_free_all(headers); return code;
}
static CURLcode sp_profile_start(CURL *easy, const sp_config *c) {
  int chrome = !strcmp(c->profile, "chrome152_preview"), firefox = !strcmp(c->profile, "firefox148");
  CURLcode code = curl_easy_impersonate(easy, chrome ? "chrome150" : firefox ? "firefox147" : c->profile, c->default_headers);
  if (code || (!chrome && !firefox)) return code;
  if (chrome) code = curl_easy_setopt(easy, CURLOPT_TLS_TRUST_ANCHORS, sp_chrome152_anchors);
  else code = curl_easy_setopt(easy, CURLOPT_SSL_CIPHER_LIST, sp_firefox148_ciphers);
  if (code || !c->default_headers) return code;
  return chrome ? sp_profile_headers(easy, sp_chrome152_headers, sizeof sp_chrome152_headers / sizeof *sp_chrome152_headers) :
    sp_profile_headers(easy, sp_firefox148_headers, sizeof sp_firefox148_headers / sizeof *sp_firefox148_headers);
}
