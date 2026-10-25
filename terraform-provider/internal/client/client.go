// Package client is a small HTTP client for the Shoal API.
package client

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// Client talks to one Shoal server as one principal.
type Client struct {
	Endpoint  string
	APIKey    string
	Tenant    string // only with the system admin token
	UserAgent string
	HTTP      *http.Client
}

// New returns a client for endpoint (e.g. https://shoal.example.com).
func New(endpoint, apiKey, tenant, userAgent string) *Client {
	return &Client{
		Endpoint:  strings.TrimRight(endpoint, "/"),
		APIKey:    apiKey,
		Tenant:    tenant,
		UserAgent: userAgent,
		HTTP:      &http.Client{Timeout: 60 * time.Second},
	}
}

// Error is an API problem document (RFC 9457).
type Error struct {
	Status  int            `json:"status"`
	Code    string         `json:"code"`
	Detail  string         `json:"detail"`
	Details map[string]any `json:"details,omitempty"`
	Errors  []any          `json:"errors,omitempty"`
}

func (e *Error) Error() string {
	msg := fmt.Sprintf("%d %s: %s", e.Status, e.Code, e.Detail)
	if errs, ok := e.Details["errors"]; ok {
		b, _ := json.Marshal(errs)
		msg += " " + string(b)
	}
	if len(e.Errors) > 0 {
		b, _ := json.Marshal(e.Errors)
		msg += " " + string(b)
	}
	return msg
}

// IsNotFound reports whether err is a 404 from the API.
func IsNotFound(err error) bool {
	var apiErr *Error
	return errors.As(err, &apiErr) && apiErr.Status == http.StatusNotFound
}

// Do sends a request and decodes a JSON response into out (if non-nil).
// It returns the HTTP status code.
func (c *Client) Do(ctx context.Context, method, path string, body, out any) (int, error) {
	var reader io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return 0, err
		}
		reader = bytes.NewReader(data)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.Endpoint+path, reader)
	if err != nil {
		return 0, err
	}
	req.Header.Set("Accept", "application/json")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if c.APIKey != "" {
		req.Header.Set("Authorization", "Bearer "+c.APIKey)
	}
	if c.Tenant != "" {
		req.Header.Set("X-AWM-Tenant", c.Tenant)
	}
	if c.UserAgent != "" {
		req.Header.Set("User-Agent", c.UserAgent)
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(resp.Body)
	if err != nil {
		return resp.StatusCode, err
	}
	if resp.StatusCode >= 400 {
		apiErr := &Error{Status: resp.StatusCode, Detail: strings.TrimSpace(string(data))}
		if json.Unmarshal(data, apiErr) == nil && apiErr.Detail == "" {
			apiErr.Detail = http.StatusText(resp.StatusCode)
		}
		apiErr.Status = resp.StatusCode
		return resp.StatusCode, apiErr
	}
	if out != nil && len(data) > 0 {
		if err := json.Unmarshal(data, out); err != nil {
			return resp.StatusCode, fmt.Errorf("decoding %s %s: %w", method, path, err)
		}
	}
	return resp.StatusCode, nil
}

// Path joins escaped path segments: Path("v1", "namespaces", ns) -> /v1/namespaces/<ns>.
func Path(segments ...string) string {
	var b strings.Builder
	for _, s := range segments {
		b.WriteString("/")
		b.WriteString(url.PathEscape(s))
	}
	return b.String()
}

// Resource is the shape of every declarative resource (workflows, agents, ...).
type Resource struct {
	APIVersion string           `json:"apiVersion,omitempty"`
	Kind       string           `json:"kind,omitempty"`
	Metadata   ResourceMetadata `json:"metadata"`
	Spec       json.RawMessage  `json:"spec"`
	Status     map[string]any   `json:"status,omitempty"`
}

// ResourceMetadata is a resource's metadata; server fields are filled on reads.
type ResourceMetadata struct {
	Name        string            `json:"name,omitempty"`
	Namespace   string            `json:"namespace,omitempty"`
	Description *string           `json:"description,omitempty"`
	Labels      map[string]string `json:"labels,omitempty"`
	Version     int64             `json:"version,omitempty"`
	CreatedAt   string            `json:"created_at,omitempty"`
	UpdatedAt   string            `json:"updated_at,omitempty"`
	UpdatedBy   string            `json:"updated_by,omitempty"`
}

type Namespace struct {
	Name        string  `json:"name,omitempty"`
	DisplayName *string `json:"display_name,omitempty"`
	Description *string `json:"description,omitempty"`
	CreatedAt   string  `json:"created_at,omitempty"`
}

type Secret struct {
	Name      string `json:"name"`
	Namespace string `json:"namespace"`
	CreatedAt string `json:"created_at"`
	UpdatedAt string `json:"updated_at"`
}

type Role struct {
	Name        string   `json:"name,omitempty"`
	Description *string  `json:"description,omitempty"`
	Permissions []string `json:"permissions"`
	Builtin     bool     `json:"builtin,omitempty"`
}

type ServiceAccount struct {
	ID          string  `json:"id,omitempty"`
	Name        string  `json:"name,omitempty"`
	DisplayName *string `json:"display_name,omitempty"`
	Description *string `json:"description,omitempty"`
}

type User struct {
	ID          string   `json:"id,omitempty"`
	Username    string   `json:"username,omitempty"`
	Email       *string  `json:"email,omitempty"`
	DisplayName *string  `json:"display_name,omitempty"`
	Active      *bool    `json:"active,omitempty"`
	ExternalID  *string  `json:"external_id,omitempty"`
	Groups      []string `json:"groups,omitempty"`
}

type Group struct {
	ID          string   `json:"id,omitempty"`
	DisplayName string   `json:"display_name,omitempty"`
	Source      string   `json:"source,omitempty"`
	Members     []string `json:"members"`
}

type RoleBinding struct {
	ID          string  `json:"id,omitempty"`
	Role        string  `json:"role"`
	SubjectKind string  `json:"subject_kind"`
	SubjectID   string  `json:"subject_id"`
	Namespace   *string `json:"namespace"`
}

type APIKey struct {
	ID         string   `json:"id,omitempty"`
	Name       string   `json:"name"`
	Prefix     string   `json:"prefix,omitempty"`
	Scopes     []string `json:"scopes"`
	Namespaces []string `json:"namespaces"`
	OwnerID    string   `json:"owner_id,omitempty"`
	ExpiresAt  *string  `json:"expires_at,omitempty"`
	RevokedAt  *string  `json:"revoked_at,omitempty"`
}

type APIKeyIn struct {
	Name           string   `json:"name"`
	ServiceAccount *string  `json:"service_account,omitempty"`
	Scopes         []string `json:"scopes"`
	Namespaces     []string `json:"namespaces"`
	ExpiresAt      *string  `json:"expires_at,omitempty"`
}

type APIKeyCreated struct {
	Key   APIKey `json:"key"`
	Token string `json:"token"`
}

type OIDCProvider struct {
	Name             string   `json:"name,omitempty"`
	DisplayName      *string  `json:"display_name,omitempty"`
	Issuer           string   `json:"issuer"`
	ClientID         string   `json:"client_id"`
	ClientSecret     *string  `json:"client_secret,omitempty"`
	HasClientSecret  bool     `json:"has_client_secret,omitempty"`
	Scopes           []string `json:"scopes"`
	GroupsClaim      *string  `json:"groups_claim"`
	AllowedDomains   []string `json:"allowed_domains"`
	AutoProvision    bool     `json:"auto_provision"`
	DefaultRole      *string  `json:"default_role"`
	DefaultNamespace *string  `json:"default_namespace"`
	Enabled          bool     `json:"enabled"`
}

type Me struct {
	Actor       string   `json:"actor"`
	Kind        string   `json:"kind"`
	UserID      *string  `json:"user_id"`
	DisplayName *string  `json:"display_name"`
	Email       *string  `json:"email"`
	IsSystem    bool     `json:"is_system_admin"`
	Namespaces  []string `json:"namespaces"`
	Tenant      *Tenant  `json:"tenant"`
}

type Tenant struct {
	ID   string `json:"id"`
	Slug string `json:"slug"`
	Name string `json:"name"`
}

type Permission struct {
	Name        string `json:"name"`
	Description string `json:"description"`
	Scope       string `json:"scope"`
}
