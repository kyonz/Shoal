{{- define "awm.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "awm.fullname" -}}
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

{{- define "awm.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "awm.labels" -}}
helm.sh/chart: {{ include "awm.chart" . }}
{{ include "awm.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "awm.selectorLabels" -}}
app.kubernetes.io/name: {{ include "awm.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "awm.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "awm.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{- define "awm.image" -}}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) }}
{{- end }}

{{- define "awm.secretName" -}}
{{- default (include "awm.fullname" .) .Values.secrets.existingSecret }}
{{- end }}

{{- define "awm.postgresql.fullname" -}}
{{- printf "%s-postgresql" (include "awm.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/* Environment shared by the API and worker containers. */}}
{{- define "awm.env" -}}
- name: AWM_SECRET_KEY
  valueFrom: {secretKeyRef: {name: {{ include "awm.secretName" . }}, key: AWM_SECRET_KEY}}
- name: AWM_ADMIN_TOKEN
  valueFrom: {secretKeyRef: {name: {{ include "awm.secretName" . }}, key: AWM_ADMIN_TOKEN}}
- name: AWM_DATABASE_URL
  valueFrom: {secretKeyRef: {name: {{ include "awm.secretName" . }}, key: AWM_DATABASE_URL}}
- name: AWM_WORKER_ID
  valueFrom: {fieldRef: {fieldPath: metadata.name}}
{{- with .Values.extraEnv }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{- define "awm.envFrom" -}}
- configMapRef:
    name: {{ include "awm.fullname" . }}
{{- with .Values.extraEnvFrom }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{/* Writable scratch space under a read-only root filesystem. */}}
{{- define "awm.volumes" -}}
- name: tmp
  emptyDir: {}
- name: sandboxes
  emptyDir:
    sizeLimit: {{ .Values.worker.sandboxVolume.sizeLimit }}
{{- end }}

{{- define "awm.volumeMounts" -}}
- name: tmp
  mountPath: /tmp
- name: sandboxes
  mountPath: /var/lib/awm/sandboxes
{{- end }}
