package transfer

import (
	"context"
	"net/http"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/s-zhi/subtitles-ai-drive/internal/config"
	driveclient "github.com/s-zhi/subtitles-ai-drive/internal/drive"
	"github.com/s-zhi/subtitles-ai-drive/internal/store"
)

type fakeDrive struct {
	mu         sync.Mutex
	parentID   string
	properties map[string]string
}

func (f *fakeDrive) StartUploadSessionInFolder(_ context.Context, _ string, _ string, _ int64, parentID string, properties map[string]string) (driveclient.UploadSession, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.parentID = parentID
	f.properties = map[string]string{}
	for key, value := range properties {
		f.properties[key] = value
	}
	return driveclient.UploadSession{URL: "fake://session"}, nil
}

func (f *fakeDrive) UploadChunk(_ context.Context, _ string, _ *os.File, _ int64, total, _ int64) (driveclient.ChunkResult, error) {
	return driveclient.ChunkResult{NextOffset: total, Completed: true, File: &driveclient.File{ID: "drive-file-1"}}, nil
}

func (f *fakeDrive) Metadata(_ context.Context, _ string) (*driveclient.File, error) {
	return &driveclient.File{Name: "file.bin", MimeType: "application/octet-stream", Size: 1}, nil
}

func (f *fakeDrive) DownloadRange(_ context.Context, _ string, _ string) (*driveclient.File, *http.Response, error) {
	return nil, nil, os.ErrInvalid
}

func TestBatchDriveUploadPropagatesMetadataAndAggregates(t *testing.T) {
	t.Parallel()
	cfg := config.Default()
	cfg.DataDir = t.TempDir()
	state, err := store.Open(filepath.Join(cfg.DataDir, "state.json"))
	if err != nil {
		t.Fatal(err)
	}
	drive := &fakeDrive{}
	manager := NewManager(&cfg, state, drive)
	batch, _, err := state.CreateFolderBatch("request-1", "root-folder", 1, 1)
	if err != nil {
		t.Fatal(err)
	}
	entry, err := state.CreateFolderEntry(store.FolderEntry{
		BatchID: batch.ID, RelativePath: "nested/file.bin", Name: "file.bin", Size: 1,
		ParentID: "nested-folder", State: StatePending,
	})
	if err != nil {
		t.Fatal(err)
	}
	staging := filepath.Join(cfg.DataDir, "file.part")
	if err := os.WriteFile(staging, []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	upload, err := state.CreateUploadWithMetadata(staging, "file.bin", "application/octet-stream", 1, batch.ID, entry.ID, entry.ParentID, entry.RelativePath)
	if err != nil {
		t.Fatal(err)
	}
	upload, err = state.UpdateUpload(upload.ID, func(current *store.Upload) error {
		current.Offset = 1
		current.Completed = true
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := manager.StartDriveUpload(upload); err != nil {
		t.Fatal(err)
	}

	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		current, ok := state.GetFolderEntry(entry.ID)
		if ok && current.State == StateSuccess {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	gotEntry, _ := state.GetFolderEntry(entry.ID)
	if gotEntry.State != StateSuccess || gotEntry.FileID != "drive-file-1" {
		t.Fatalf("entry = %#v", gotEntry)
	}
	gotBatch, _ := state.GetFolderBatch(batch.ID)
	if gotBatch.State != StateSuccess || gotBatch.CompletedEntries != 1 || gotBatch.CompletedBytes != 1 {
		t.Fatalf("batch = %#v", gotBatch)
	}
	drive.mu.Lock()
	defer drive.mu.Unlock()
	if drive.parentID != entry.ParentID {
		t.Fatalf("parent id = %q, want %q", drive.parentID, entry.ParentID)
	}
	if drive.properties["subtitles_ai_batch_id"] != batch.ID || drive.properties["subtitles_ai_entry_id"] != entry.ID {
		t.Fatalf("upload properties = %#v", drive.properties)
	}
}

func TestCancelBatchKeepsSuccessfulEntriesAndCancelsRemaining(t *testing.T) {
	t.Parallel()
	cfg := config.Default()
	cfg.DataDir = t.TempDir()
	state, err := store.Open(filepath.Join(cfg.DataDir, "state.json"))
	if err != nil {
		t.Fatal(err)
	}
	manager := NewManager(&cfg, state, &fakeDrive{})
	batch, _, err := state.CreateFolderBatch("cancel-partial", "root-folder", 2, 2)
	if err != nil {
		t.Fatal(err)
	}
	success, err := state.CreateFolderEntry(store.FolderEntry{
		BatchID: batch.ID, RelativePath: "done.bin", Name: "done.bin", Size: 1, State: StateSuccess,
	})
	if err != nil {
		t.Fatal(err)
	}
	pending, err := state.CreateFolderEntry(store.FolderEntry{
		BatchID: batch.ID, RelativePath: "pending.bin", Name: "pending.bin", Size: 1, State: StatePending,
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := manager.CancelBatch(batch.ID); err != nil {
		t.Fatal(err)
	}
	gotSuccess, _ := state.GetFolderEntry(success.ID)
	gotPending, _ := state.GetFolderEntry(pending.ID)
	if gotSuccess.State != StateSuccess || gotPending.State != StateCancelled {
		t.Fatalf("success=%#v pending=%#v", gotSuccess, gotPending)
	}
	gotBatch, _ := state.GetFolderBatch(batch.ID)
	if gotBatch.State != StateCancelled || gotBatch.CompletedEntries != 1 {
		t.Fatalf("batch = %#v", gotBatch)
	}
}

func TestMarkTransferTransferringGuardsTerminalAndPausedStates(t *testing.T) {
	t.Parallel()
	cfg := config.Default()
	cfg.DataDir = t.TempDir()
	state, err := store.Open(filepath.Join(cfg.DataDir, "state.json"))
	if err != nil {
		t.Fatal(err)
	}
	manager := NewManager(&cfg, state, &fakeDrive{})

	guardedStates := []string{StatePaused, StateCancelled, StateSuccess}
	for _, st := range guardedStates {
		tr, err := state.CreateTransfer(store.Transfer{
			Kind:  KindDriveUpload,
			State: st,
		})
		if err != nil {
			t.Fatal(err)
		}
		if err := manager.markTransferTransferring(tr.ID); err != nil {
			t.Fatalf("markTransferTransferring error: %v", err)
		}
		updated, ok := state.GetTransfer(tr.ID)
		if !ok || updated.State != st {
			t.Fatalf("state for %s changed to %s, want %s", st, updated.State, st)
		}
	}
}

type blockingDrive struct {
	fakeDrive
	uploadChunkStarted chan struct{}
	allowUploadChunk   chan struct{}
}

func (b *blockingDrive) UploadChunk(ctx context.Context, url string, f *os.File, off, total, chunk int64) (driveclient.ChunkResult, error) {
	select {
	case b.uploadChunkStarted <- struct{}{}:
	default:
	}
	select {
	case <-b.allowUploadChunk:
	case <-ctx.Done():
		return driveclient.ChunkResult{}, ctx.Err()
	}
	return b.fakeDrive.UploadChunk(ctx, url, f, off, total, chunk)
}

func TestPauseDuringUploadDoesNotGetOverwrittenByWorker(t *testing.T) {
	t.Parallel()
	cfg := config.Default()
	cfg.DataDir = t.TempDir()
	state, err := store.Open(filepath.Join(cfg.DataDir, "state.json"))
	if err != nil {
		t.Fatal(err)
	}

	drive := &blockingDrive{
		uploadChunkStarted: make(chan struct{}, 1),
		allowUploadChunk:   make(chan struct{}),
	}
	manager := NewManager(&cfg, state, drive)

	staging := filepath.Join(cfg.DataDir, "file.part")
	if err := os.WriteFile(staging, []byte("test bytes"), 0o600); err != nil {
		t.Fatal(err)
	}
	upload, err := state.CreateUpload(staging, "file.bin", "application/octet-stream", int64(len("test bytes")))
	if err != nil {
		t.Fatal(err)
	}
	upload, err = state.UpdateUpload(upload.ID, func(u *store.Upload) error { u.Completed = true; return nil })
	if err != nil {
		t.Fatal(err)
	}

	tr, err := manager.StartDriveUpload(upload)
	if err != nil {
		t.Fatal(err)
	}

	// Wait until worker enters UploadChunk
	select {
	case <-drive.uploadChunkStarted:
	case <-time.After(2 * time.Second):
		t.Fatal("timed out waiting for UploadChunk to start")
	}

	// User pauses the transfer while chunk upload is blocked
	if err := manager.Pause(tr.ID); err != nil {
		t.Fatalf("Pause failed: %v", err)
	}

	// Allow chunk upload to proceed/finish context cancellation
	close(drive.allowUploadChunk)

	// Wait briefly for worker goroutine to exit
	time.Sleep(50 * time.Millisecond)

	finalTr, ok := state.GetTransfer(tr.ID)
	if !ok {
		t.Fatal("transfer not found")
	}
	if finalTr.State != StatePaused {
		t.Fatalf("expected transfer state %s, got %s", StatePaused, finalTr.State)
	}
}

func TestManagerRecoverDoesNotStartPausedOrTerminalTransfers(t *testing.T) {
	t.Parallel()
	cfg := config.Default()
	cfg.DataDir = t.TempDir()
	state, err := store.Open(filepath.Join(cfg.DataDir, "state.json"))
	if err != nil {
		t.Fatal(err)
	}

	manager := NewManager(&cfg, state, &fakeDrive{})

	// Create transfers in PAUSED, SUCCESS, FAILED, CANCELLED states
	nonRecoverable := []string{StatePaused, StateSuccess, StateFailed, StateCancelled}
	nonRecoverableIDs := make(map[string]bool)
	for _, st := range nonRecoverable {
		tr, err := state.CreateTransfer(store.Transfer{
			Kind:  KindDriveUpload,
			State: st,
		})
		if err != nil {
			t.Fatal(err)
		}
		nonRecoverableIDs[tr.ID] = true
	}

	// Trigger Recover()
	manager.Recover()

	// Verify that m.cancels map never registered any non-recoverable transfer IDs
	manager.mu.Lock()
	defer manager.mu.Unlock()
	for id := range nonRecoverableIDs {
		if _, exists := manager.cancels[id]; exists {
			t.Fatalf("transfer %s (non-recoverable) was registered in cancels during Recover()", id)
		}
	}
}

func TestPostPythonUploadRespectsTimeoutAndCancellation(t *testing.T) {
	t.Parallel()
	cfg := config.Default()
	cfg.DataDir = t.TempDir()
	cfg.PythonTimeoutSeconds = 1
	state, err := store.Open(filepath.Join(cfg.DataDir, "state.json"))
	if err != nil {
		t.Fatal(err)
	}
	manager := NewManager(&cfg, state, &fakeDrive{})

	cancelCtx, cancel := context.WithCancel(context.Background())
	cancel()
	staging := filepath.Join(cfg.DataDir, "import-file.bin")
	if err := os.WriteFile(staging, []byte("test payload data"), 0o600); err != nil {
		t.Fatal(err)
	}

	if err := manager.postPythonUpload(cancelCtx, "transfer-1", staging, "import-file.bin"); err == nil {
		t.Fatal("expected error when context is cancelled, got nil")
	}
}
