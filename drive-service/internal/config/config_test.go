package config

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// TestLoadAppliesSafeDefaultsAndDriveChunkAlignment 验证配置默认值和 Drive 分片对齐规则。
func TestLoadAppliesSafeDefaultsAndDriveChunkAlignment(t *testing.T) {
	t.Parallel()
	tmp := t.TempDir()
	configPath := filepath.Join(tmp, "config.json")
	payload, err := json.Marshal(map[string]any{
		"data_dir":         filepath.Join(tmp, "data"),
		"chunk_size_bytes": 300000,
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(configPath, payload, 0o600); err != nil {
		t.Fatal(err)
	}
	cfg, err := Load(configPath)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.ChunkSizeBytes != driveChunkMultiple {
		t.Fatalf("chunk size = %d, want %d", cfg.ChunkSizeBytes, driveChunkMultiple)
	}
	if cfg.PythonTimeoutSeconds != 1800 {
		t.Fatalf("python timeout = %d, want 1800", cfg.PythonTimeoutSeconds)
	}
	if len(cfg.GoogleScopes) != 1 || cfg.GoogleScopes[0] != defaultScope {
		t.Fatalf("unexpected scopes: %#v", cfg.GoogleScopes)
	}
	if err := cfg.EnsureDataDir(); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(cfg.StatePath()); !os.IsNotExist(err) {
		t.Fatalf("state path should not be created by EnsureDataDir: %v", err)
	}
}

func TestLoadAppliesFolderUploadResourceDefaults(t *testing.T) {
	t.Parallel()
	tmp := t.TempDir()
	configPath := filepath.Join(tmp, "config.json")
	if err := os.WriteFile(configPath, []byte(`{"max_folder_entries":0,"max_folder_bytes":-1,"max_manifest_bytes":0,"max_concurrent_transfers":0}`), 0o600); err != nil {
		t.Fatal(err)
	}
	cfg, err := Load(configPath)
	if err != nil {
		t.Fatal(err)
	}
	defaults := Default()
	if cfg.MaxFolderEntries != defaults.MaxFolderEntries || cfg.MaxFolderBytes != defaults.MaxFolderBytes ||
		cfg.MaxManifestBytes != defaults.MaxManifestBytes || cfg.MaxFolderDepth != defaults.MaxFolderDepth ||
		cfg.MaxFolderDirectories != defaults.MaxFolderDirectories || cfg.MaxConcurrentTransfers != defaults.MaxConcurrentTransfers {
		t.Fatalf("folder defaults = %#v, want %#v", cfg, defaults)
	}
}
