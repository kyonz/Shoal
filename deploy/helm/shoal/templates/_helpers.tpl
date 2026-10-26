{{- define "shoal.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "shoal.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "shoal.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "shoal.labels" -}}
helm.sh/chart: {{ include "shoal.chart" . }}
{{ include "shoal.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "shoal.selectorLabels" -}}
app.kubernetes.io/name: {{ include "shoal.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "shoal.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "shoal.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{- define "shoal.image" -}}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) }}
{{- end }}

{{- define "shoal.secretName" -}}
{{- default (include "shoal.fullname" .) .Values.secrets.existingSecret }}
{{- end }}

{{- define "shoal.postgresql.fullname" -}}
{{- printf "%s-postgresql" (include "shoal.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "shoal.pgbouncer.fullname" -}}
{{- printf "%s-pgbouncer" (include "shoal.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/* True when Shoal reaches Postgres through a transaction-mode pooler. */}}
{{- define "shoal.pgbouncer.inUse" -}}
{{- if .Values.postgresql.enabled }}{{ .Values.pgbouncer.enabled }}{{ else }}{{ .Values.database.pgbouncer }}{{ end }}
{{- end }}

{{/* Environment shared by the API and worker containers. */}}
{{- define "shoal.env" -}}
- name: SHOAL_SECRET_KEY
  valueFrom: {secretKeyRef: {name: {{ include "shoal.secretName" . }}, key: SHOAL_SECRET_KEY}}
- name: SHOAL_ADMIN_TOKEN
  valueFrom: {secretKeyRef: {name: {{ include "shoal.secretName" . }}, key: SHOAL_ADMIN_TOKEN}}
- name: SHOAL_DATABASE_URL
  valueFrom: {secretKeyRef: {name: {{ include "shoal.secretName" . }}, key: SHOAL_DATABASE_URL}}
- name: SHOAL_DATABASE_DIRECT_URL
  valueFrom: {secretKeyRef: {name: {{ include "shoal.secretName" . }}, key: SHOAL_DATABASE_DIRECT_URL, optional: true}}
- name: SHOAL_WORKER_ID
  valueFrom: {fieldRef: {fieldPath: metadata.name}}
{{- with .Values.orchestrator.temporal.apiKeySecret }}
{{- if and (eq $.Values.orchestrator.type "temporal") .name }}
- name: SHOAL_TEMPORAL_API_KEY
  valueFrom: {secretKeyRef: {name: {{ .name }}, key: {{ .key }}}}
{{- end }}
{{- end }}
{{- with .Values.instance.oidc }}
{{- if .issuer }}
- name: SHOAL_INSTANCE_OIDC
  value: {{ dict "issuer" .issuer "client_id" .clientId "display_name" .displayName "allowed_domains" .allowedDomains | toJson | quote }}
{{- if .clientSecretRef.name }}
- name: SHOAL_INSTANCE_OIDC_CLIENT_SECRET
  valueFrom: {secretKeyRef: {name: {{ .clientSecretRef.name }}, key: {{ .clientSecretRef.key }}}}
{{- else if .clientSecret }}
- name: SHOAL_INSTANCE_OIDC_CLIENT_SECRET
  valueFrom: {secretKeyRef: {name: {{ include "shoal.secretName" $ }}, key: SHOAL_INSTANCE_OIDC_CLIENT_SECRET}}
{{- end }}
{{- end }}
{{- end }}
{{- with .Values.extraEnv }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{- define "shoal.envFrom" -}}
- configMapRef:
    name: {{ include "shoal.fullname" . }}
{{- with .Values.extraEnvFrom }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{/* Writable scratch space under a read-only root filesystem. */}}
{{- define "shoal.volumes" -}}
- name: tmp
  emptyDir: {}
- name: sandboxes
  emptyDir:
    sizeLimit: {{ .Values.worker.sandboxVolume.sizeLimit }}
{{- if and (eq .Values.orchestrator.type "temporal") .Values.orchestrator.temporal.mtlsSecret }}
- name: temporal-tls
  secret:
    secretName: {{ .Values.orchestrator.temporal.mtlsSecret }}
{{- end }}
{{- end }}

{{- define "shoal.volumeMounts" -}}
- name: tmp
  mountPath: /tmp
- name: sandboxes
  mountPath: /var/lib/shoal/sandboxes
{{- if and (eq .Values.orchestrator.type "temporal") .Values.orchestrator.temporal.mtlsSecret }}
- name: temporal-tls
  mountPath: /etc/shoal/temporal-tls
  readOnly: true
{{- end }}
{{- end }}
