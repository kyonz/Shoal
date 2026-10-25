package provider

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
	"testing"
	"time"

	"github.com/hashicorp/terraform-plugin-framework/providerserver"
	"github.com/hashicorp/terraform-plugin-go/tfprotov6"

	"github.com/kyonz/ai-workflow-manager/terraform-provider/internal/client"
)

// Acceptance tests run against a real Shoal server. Point them at one with
// SHOAL_ENDPOINT and SHOAL_API_KEY, or leave those unset and TestMain starts
// the engine from ../engine (uv + uvicorn, SQLite) and creates a tenant.

var testAccProtoV6ProviderFactories = map[string]func() (tfprotov6.ProviderServer, error){
	"shoal": providerserver.NewProtocol6WithError(New("test")()),
}

func TestMain(m *testing.M) {
	if os.Getenv("TF_ACC") == "" || os.Getenv("SHOAL_ENDPOINT") != "" {
		os.Exit(m.Run())
	}
	stop, err := startEngine()
	if err != nil {
		fmt.Fprintln(os.Stderr, "starting the Shoal engine for acceptance tests:", err)
		os.Exit(1)
	}
	code := m.Run()
	stop()
	os.Exit(code)
}

func startEngine() (func(), error) {
	engineDir, err := filepath.Abs(filepath.Join("..", "..", "..", "engine"))
	if err != nil {
		return nil, err
	}
	tmp, err := os.MkdirTemp("", "shoal-acc-")
	if err != nil {
		return nil, err
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return nil, err
	}
	port := listener.Addr().(*net.TCPAddr).Port
	listener.Close()
	endpoint := fmt.Sprintf("http://127.0.0.1:%d", port)
	const admin = "acc-admin-token"

	cmd := exec.Command("uv", "run", "--quiet", "uvicorn", "--factory", "shoal.api.app:create_app",
		"--host", "127.0.0.1", "--port", fmt.Sprint(port), "--log-level", "warning")
	cmd.Dir = engineDir
	cmd.Env = append(os.Environ(),
		"SHOAL_DATABASE_URL=sqlite+aiosqlite:///"+filepath.Join(tmp, "shoal.db"),
		"SHOAL_ADMIN_TOKEN="+admin,
		"SHOAL_PUBLIC_URL="+endpoint,
		"SHOAL_LOCAL_SANDBOX_ROOT="+filepath.Join(tmp, "sandboxes"),
	)
	logFile, err := os.Create(filepath.Join(tmp, "engine.log"))
	if err != nil {
		return nil, err
	}
	cmd.Stdout, cmd.Stderr = logFile, logFile
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true} // so uv's child uvicorn is stopped too
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	stop := func() {
		_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGTERM)
		_, _ = cmd.Process.Wait()
		logFile.Close()
		_ = os.RemoveAll(tmp)
	}
	deadline := time.Now().Add(60 * time.Second)
	for {
		resp, err := http.Get(endpoint + "/healthz")
		if err == nil && resp.StatusCode == 200 {
			resp.Body.Close()
			break
		}
		if time.Now().After(deadline) {
			stop()
			return nil, fmt.Errorf("engine did not become healthy at %s", endpoint)
		}
		time.Sleep(300 * time.Millisecond)
	}
	body, _ := json.Marshal(map[string]string{"slug": "acc", "name": "Acceptance"})
	req, _ := http.NewRequest(http.MethodPost, endpoint+"/v1/tenants", bytes.NewReader(body))
	req.Header.Set("Authorization", "Bearer "+admin)
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		stop()
		return nil, err
	}
	defer resp.Body.Close()
	var created struct {
		APIKey string `json:"api_key"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&created); err != nil || created.APIKey == "" {
		stop()
		return nil, fmt.Errorf("creating the acceptance tenant: HTTP %d", resp.StatusCode)
	}
	os.Setenv("SHOAL_ENDPOINT", endpoint)
	os.Setenv("SHOAL_API_KEY", created.APIKey)
	os.Setenv("SHOAL_ACC_ADMIN_TOKEN", admin)
	return stop, nil
}

func testAccPreCheck(t *testing.T) {
	t.Helper()
	for _, env := range []string{"SHOAL_ENDPOINT", "SHOAL_API_KEY"} {
		if os.Getenv(env) == "" {
			t.Fatalf("%s must be set for acceptance tests", env)
		}
	}
}

func clientFromEnv(token string) *client.Client {
	return client.New(os.Getenv("SHOAL_ENDPOINT"), token, "", "acc-test")
}

func testContext() context.Context { return context.Background() }
