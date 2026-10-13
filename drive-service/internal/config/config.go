// Package config 负责加载并规范化 Drive sidecar 的本地配置。
// OAuth 凭据和 Token 始终置于源码之外，同时为单用户部署提供稳定默认值。
package config

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
)

const defaultScope = "https://www.googleapis.com/auth/drive.file"

const driveChunkMultiple = int64(256 * 1024)

// Config 保存单用户 Drive sidecar 所需的进程配置。示例配置中的凭据特意留空，
// 可通过本地 OAuth 客户端 JSON 文件提供。
type Config struct {
	ListenAddr             string   `json:"listen_addr"`
	DataDir                string   `json:"data_dir"`
	PythonBaseURL          string   `json:"python_base_url"`
	PythonAPIToken         string   `json:"python_api_token"`
	GoogleClientID         string   `json:"google_client_id"`
	GoogleClientSecret     string   `json:"google_client_secret"`
	GoogleClientConfigFile string   `json:"google_client_config_file"`
	GoogleScopes           []string `json:"google_scopes"`
	DriveFolderID          string   `json:"drive_folder_id"`
	MaxUploadBytes         int64    `json:"max_upload_bytes"`
	MaxFolderEntries       int      `json:"max_folder_entries"`
	MaxFolderBytes         int64    `json:"max_folder_bytes"`
	MaxManifestBytes       int64    `json:"max_manifest_bytes"`
	MaxFolderDepth         int      `json:"max_folder_depth"`
	MaxFolderDirectories   int      `json:"max_folder_directories"`
	MaxConcurrentTransfers int      `json:"max_concurrent_transfers"`
	ChunkSizeBytes         int64    `json:"chunk_size_bytes"`
	RequestTimeoutSeconds  int      `json:"request_timeout_seconds"`
	PythonTimeoutSeconds   int      `json:"python_timeout_seconds"`
}

// Default 返回适合本地开发的 sidecar 安全默认配置。
func Default() Config {
	return Config{
		ListenAddr:             "127.0.0.1:8787",
		DataDir:                "./drive-data",
		PythonBaseURL:          "http://127.0.0.1:8000",
		GoogleScopes:           []string{defaultScope},
		MaxUploadBytes:         2 * 1024 * 1024 * 1024,
		MaxFolderEntries:       1000,
		MaxFolderBytes:         2 * 1024 * 1024 * 1024,
		MaxManifestBytes:       8 * 1024 * 1024,
		MaxFolderDepth:         64,
		MaxFolderDirectories:   5000,
		MaxConcurrentTransfers: 3,
		ChunkSizeBytes:         16 * 1024 * 1024,
		RequestTimeoutSeconds:  600,
		PythonTimeoutSeconds:   1800,
	}
}

// Load 从 path 读取 JSON 配置，并为缺失或无效的非敏感字段应用默认值。
// path 为空时直接返回 Default，不执行文件 I/O。
func Load(path string) (Config, error) {
	cfg := Default()
	if path == "" {
		return cfg, nil
	}
	b, err := os.ReadFile(path)
	if err != nil {
		return cfg, fmt.Errorf("read config %s: %w", path, err)
	}
	if err := json.Unmarshal(b, &cfg); err != nil {
		return cfg, fmt.Errorf("parse config %s: %w", path, err)
	}
	if cfg.ListenAddr == "" {
		cfg.ListenAddr = Default().ListenAddr
	}
	if cfg.DataDir == "" {
		cfg.DataDir = Default().DataDir
	}
	if cfg.PythonBaseURL == "" {
		cfg.PythonBaseURL = Default().PythonBaseURL
	}
	if len(cfg.GoogleScopes) == 0 {
		cfg.GoogleScopes = []string{defaultScope}
	}
	if cfg.MaxUploadBytes <= 0 {
		cfg.MaxUploadBytes = Default().MaxUploadBytes
	}
	if cfg.MaxFolderEntries <= 0 {
		cfg.MaxFolderEntries = Default().MaxFolderEntries
	}
	if cfg.MaxFolderBytes <= 0 {
		cfg.MaxFolderBytes = Default().MaxFolderBytes
	}
	if cfg.MaxManifestBytes <= 0 {
		cfg.MaxManifestBytes = Default().MaxManifestBytes
	}
	if cfg.MaxFolderDepth <= 0 {
		cfg.MaxFolderDepth = Default().MaxFolderDepth
	}
	if cfg.MaxFolderDirectories <= 0 {
		cfg.MaxFolderDirectories = Default().MaxFolderDirectories
	}
	if cfg.MaxConcurrentTransfers <= 0 {
		cfg.MaxConcurrentTransfers = Default().MaxConcurrentTransfers
	}
	if cfg.ChunkSizeBytes <= 0 {
		cfg.ChunkSizeBytes = Default().ChunkSizeBytes
	}
	// Drive 断点上传要求除最后一块外，每个分片大小必须是 256 KiB 的倍数。
	// 这里统一修正过小或未对齐的配置，避免配置笔误导致大文件上传到一半
	// 才收到远端 400 错误。
	if cfg.ChunkSizeBytes < driveChunkMultiple {
		cfg.ChunkSizeBytes = driveChunkMultiple
	} else {
		cfg.ChunkSizeBytes -= cfg.ChunkSizeBytes % driveChunkMultiple
		if cfg.ChunkSizeBytes < driveChunkMultiple {
			cfg.ChunkSizeBytes = driveChunkMultiple
		}
	}
	if cfg.RequestTimeoutSeconds <= 0 {
		cfg.RequestTimeoutSeconds = Default().RequestTimeoutSeconds
	}
	if cfg.PythonTimeoutSeconds <= 0 {
		cfg.PythonTimeoutSeconds = Default().PythonTimeoutSeconds
	}
	return cfg, nil
}

// EnsureDataDir 创建用于保存 OAuth 状态、传输状态和临时分片文件的私有目录。
func (c Config) EnsureDataDir() error {
	if c.DataDir == "" {
		return errors.New("data_dir is empty")
	}
	return os.MkdirAll(filepath.Clean(c.DataDir), 0o700)
}

// StatePath 返回持久化传输状态文件的路径。
func (c Config) StatePath() string {
	return filepath.Join(c.DataDir, "state.json")
}

// TokenPath 返回本地持久化 Refresh Token 的路径。
func (c Config) TokenPath() string {
	return filepath.Join(c.DataDir, "oauth_token.json")
}

// StagingDir 返回 Drive 断点下载使用的临时目录。
func (c Config) StagingDir() string {
	return filepath.Join(c.DataDir, "staging")
}

// ClientConfigPath 返回配置的 OAuth 客户端 JSON 路径；未配置时回退到
// 私有数据目录下的 oauth_client.json。
func (c Config) ClientConfigPath() string {
	if c.GoogleClientConfigFile != "" {
		return c.GoogleClientConfigFile
	}
	return filepath.Join(c.DataDir, "oauth_client.json")
}
