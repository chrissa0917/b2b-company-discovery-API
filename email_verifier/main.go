package main

import (
	"encoding/json"
	"log"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	emailverifier "github.com/AfterShip/email-verifier"
)

const sourceCommit = "d8462fdd79d9aca5452bd220f9cd5224976fea49"

type verifyRequest struct {
	Email string `json:"email"`
}

type verifyResponse struct {
	Engine       string                `json:"engine"`
	SourceCommit string                `json:"source_commit"`
	Verdict      string                `json:"verdict"`
	Result       *emailverifier.Result `json:"result,omitempty"`
	Error        string                `json:"error,omitempty"`
}

func envDurationSeconds(name string, fallback int) time.Duration {
	n, err := strconv.Atoi(os.Getenv(name))
	if err != nil || n <= 0 {
		n = fallback
	}
	return time.Duration(n) * time.Second
}

func classify(ret *emailverifier.Result, err error) string {
	if ret == nil {
		return "unknown"
	}
	if !ret.Syntax.Valid || !ret.HasMxRecords || ret.Disposable {
		return "invalid"
	}
	switch strings.ToLower(ret.Reachable) {
	case "yes":
		return "valid"
	case "no":
		return "invalid"
	}
	if ret.SMTP != nil && ret.SMTP.CatchAll {
		return "catch_all"
	}
	if err != nil {
		return "unknown"
	}
	return "unknown"
}

func main() {
	fromEmail := os.Getenv("VERIFIER_FROM_EMAIL")
	if fromEmail == "" {
		fromEmail = "verify@buyandrentrobots.com"
	}
	helloName := os.Getenv("VERIFIER_HELLO_NAME")
	if helloName == "" {
		helloName = "buyandrentrobots.com"
	}

	verifier := emailverifier.NewVerifier().
		EnableSMTPCheck().
		EnableDomainSuggest().
		FromEmail(fromEmail).
		HelloName(helloName).
		ConnectTimeout(envDurationSeconds("SMTP_CONNECT_TIMEOUT", 7)).
		OperationTimeout(envDurationSeconds("SMTP_OPERATION_TIMEOUT", 7))

	mux := http.NewServeMux()
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"ok":           true,
			"engine":       "AfterShip/email-verifier",
			"smtp_enabled": true,
		})
	})
	mux.HandleFunc("/v1/verify", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "POST required", http.StatusMethodNotAllowed)
			return
		}
		var req verifyRequest
		if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 16*1024)).Decode(&req); err != nil {
			http.Error(w, "invalid JSON", http.StatusBadRequest)
			return
		}
		req.Email = strings.TrimSpace(strings.ToLower(req.Email))
		if req.Email == "" {
			http.Error(w, "email is required", http.StatusBadRequest)
			return
		}

		ret, err := verifier.Verify(req.Email)
		resp := verifyResponse{
			Engine:       "AfterShip/email-verifier",
			SourceCommit: sourceCommit,
			Verdict:      classify(ret, err),
			Result:       ret,
		}
		if err != nil {
			resp.Error = err.Error()
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(resp)
	})

	port := os.Getenv("PORT")
	if port == "" {
		port = "8080"
	}
	srv := &http.Server{
		Addr:              ":" + port,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      40 * time.Second,
		IdleTimeout:       60 * time.Second,
	}
	log.Printf("email verifier listening on :%s", port)
	log.Fatal(srv.ListenAndServe())
}
