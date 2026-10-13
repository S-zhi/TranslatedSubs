// Package transfer 负责执行 sidecar 暴露的两条长任务流水线：本地暂存上传到
// Drive，以及从 Drive 下载后提交到现有 Python API。
package transfer

import (
	"context"
	"crypto/md5"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/s-zhi/subtitles-ai-drive/internal/config"
	driveclient "github.com/s-zhi/subtitles-ai-drive/internal/drive"
	"github.com/s-zhi/subtitles-ai-drive/internal/store"
)

const (
	KindDriveUpload   = "DRIVE_UPLOAD"
	KindPythonImport  = "PYTHON_IMPORT"
	StatePending      = "PENDING"
	StateTransferring = "TRANSFERRING"
	StateRetrying     = "RETRYING"
	StateVerifying    = "VERIFYING"
	StatePaused       = "PAUSED"
	StateSuccess      = "SUCCESS"
	StateFailed       = "FAILED"
	StateCancelled    = "CANCELLED"
)

// DriveClient is the subset of the Drive client used by the transfer worker.
// Keeping this interface small makes the state machine testable without a
// Google network connection while *drive.Client remains the production
// implementation.
type DriveClient interface {
	StartUploadSessionInFolder(context.Context, string, string, int64, string, map[string]string) (driveclient.UploadSession, error)
	UploadChunk(context.Context, string, *os.File, int64, int64, int64) (driveclient.ChunkResult, error)
	Metadata(context.Context, string) (*driveclient.File, error)
	DownloadRange(context.Context, string, string) (*driveclient.File, *http.Response, error)
}

// Manager 管理后台传输 worker 及其取消函数。每个对外可见步骤执行前，进度都会
// 先写入 Store。
type Manager struct {
	cfg   *config.Config
	store *store.Store
	drive DriveClient

	mu      sync.Mutex
	cancels map[string]context.CancelFunc
	slots   chan struct{}
}

// NewManager 创建进程级传输协调器。
func NewManager(cfg *config.Config, state *store.Store, client DriveClient) *Manager {
	if cfg == nil {
		defaultConfig := config.Default()
		cfg = &defaultConfig
	}
	limit := 3
	if cfg.MaxConcurrentTransfers > 0 {
		limit = cfg.MaxConcurrentTransfers
	}
	return &Manager{cfg: cfg, store: state, drive: client, cancels: map[string]context.CancelFunc{}, slots: make(chan struct{}, limit)}
}

// StartDriveUpload 创建一个从本地文件断点上传到 Drive 的任务。
func (m *Manager) StartDriveUpload(upload store.Upload) (store.Transfer, error) {
	if !upload.Completed || upload.Length <= 0 || upload.Path == "" {
		return store.Transfer{}, errors.New("upload staging file is incomplete")
	}
	properties := map[string]string{"subtitles_ai": "true"}
	if upload.BatchID != "" {
		properties["subtitles_ai_batch_id"] = upload.BatchID
	}
	if upload.EntryID != "" {
		properties["subtitles_ai_entry_id"] = upload.EntryID
	}
	if upload.RelativePath != "" {
		properties["subtitles_ai_relative_path"] = upload.RelativePath
	}
	t, err := m.store.CreateTransfer(store.Transfer{
		Kind:          KindDriveUpload,
		State:         StatePending,
		UploadID:      upload.ID,
		FileName:      upload.Name,
		MIME:          upload.MIME,
		LocalPath:     upload.Path,
		TotalBytes:    upload.Length,
		BatchID:       upload.BatchID,
		EntryID:       upload.EntryID,
		ParentID:      upload.ParentID,
		RelativePath:  upload.RelativePath,
		AppProperties: properties,
	})
	if err != nil {
		return store.Transfer{}, err
	}
	if upload.EntryID != "" {
		_, _ = m.store.UpdateFolderEntry(upload.EntryID, func(entry *store.FolderEntry) error {
			if entry.BatchID == "" {
				entry.BatchID = upload.BatchID
			}
			entry.UploadID = upload.ID
			entry.TransferID = t.ID
			entry.State = StatePending
			entry.Error = ""
			return nil
		})
		m.updateBatchProgress(upload.BatchID)
	}
	m.start(t.ID)
	return t, nil
}

// StartPythonImport 创建一个从 Drive 下载并导入 Python 的任务。
func (m *Manager) StartPythonImport(fileID string) (store.Transfer, error) {
	if fileID == "" {
		return store.Transfer{}, errors.New("fileId 不能为空")
	}
	// Resolve metadata before creating a task. This keeps folder imports a
	// synchronous 4xx operation instead of an asynchronously failing job.
	metadata, err := m.drive.Metadata(context.Background(), fileID)
	if err != nil {
		return store.Transfer{}, err
	}
	if metadata == nil {
		return store.Transfer{}, errors.New("Drive metadata response is empty")
	}
	if driveclient.IsFolder(metadata) {
		return store.Transfer{}, errors.New("不能把 Drive 文件夹导入 Python 流水线")
	}
	t, err := m.store.CreateTransfer(store.Transfer{
		Kind:   KindPythonImport,
		State:  StatePending,
		FileID: fileID,
	})
	if err != nil {
		return store.Transfer{}, err
	}
	m.start(t.ID)
	return t, nil
}

// Recover 重新启动进程退出时遗留的活跃传输任务（PENDING、TRANSFERRING、RETRYING、VERIFYING），排除 PAUSED 及终态任务。
func (m *Manager) Recover() {
	for _, t := range m.store.RecoverableTransfers() {
		m.start(t.ID)
	}
}

// Pause 将任务标记为暂停，并取消当前 HTTP 请求。
func (m *Manager) Pause(id string) error {
	t, ok := m.store.GetTransfer(id)
	if !ok {
		return os.ErrNotExist
	}
	if t.State == StateSuccess || t.State == StateCancelled {
		return fmt.Errorf("transfer is already %s", t.State)
	}
	if _, err := m.store.UpdateTransfer(id, func(t *store.Transfer) error { t.State = StatePaused; return nil }); err != nil {
		return err
	}
	if t.EntryID != "" {
		_, _ = m.store.UpdateFolderEntry(t.EntryID, func(entry *store.FolderEntry) error {
			entry.State = StatePaused
			return nil
		})
		m.updateBatchProgress(t.BatchID)
	}
	m.cancel(id)
	return nil
}

// Resume 使用已持久化的断点重启暂停或失败的任务。
func (m *Manager) Resume(id string) error {
	t, ok := m.store.GetTransfer(id)
	if !ok {
		return os.ErrNotExist
	}
	if t.State != StatePaused && t.State != StateFailed {
		return fmt.Errorf("transfer state %s cannot be resumed", t.State)
	}
	if _, err := m.store.UpdateTransfer(id, func(t *store.Transfer) error { t.State = StatePending; t.Error = ""; return nil }); err != nil {
		return err
	}
	if t.EntryID != "" {
		_, _ = m.store.UpdateFolderEntry(t.EntryID, func(entry *store.FolderEntry) error {
			entry.State = StatePending
			entry.Error = ""
			return nil
		})
		m.updateBatchProgress(t.BatchID)
	}
	m.start(id)
	return nil
}

// Cancel 停止传输任务并删除本地暂存文件。
func (m *Manager) Cancel(id string) error {
	t, ok := m.store.GetTransfer(id)
	if !ok {
		return os.ErrNotExist
	}
	if t.State == StateSuccess || t.State == StateCancelled {
		return fmt.Errorf("transfer is already %s", t.State)
	}
	m.cancel(id)
	if _, err := m.store.UpdateTransfer(id, func(t *store.Transfer) error { t.State = StateCancelled; return nil }); err != nil {
		return err
	}
	if t.EntryID != "" {
		_, _ = m.store.UpdateFolderEntry(t.EntryID, func(entry *store.FolderEntry) error {
			entry.State = StateCancelled
			entry.Error = ""
			return nil
		})
		m.updateBatchProgress(t.BatchID)
	}
	if t.LocalPath != "" {
		_ = os.Remove(t.LocalPath)
	}
	m.cleanupImportFiles(id)
	if t.Kind == KindDriveUpload && t.UploadID != "" {
		if upload, exists := m.store.GetUpload(t.UploadID); exists {
			_ = os.Remove(upload.Path)
		}
		_ = m.store.DeleteUpload(t.UploadID)
	}
	return nil
}

// CancelBatch cancels every active entry in a folder upload and marks entries
// which have not started yet as cancelled. It is deliberately idempotent.
func (m *Manager) CancelBatch(batchID string) error {
	batch, ok := m.store.GetFolderBatch(batchID)
	if !ok {
		return os.ErrNotExist
	}
	if batch.State == StateSuccess || batch.State == StateCancelled {
		return fmt.Errorf("folder upload is already %s", batch.State)
	}
	for _, entry := range m.store.ListFolderEntries(batchID) {
		if entry.State == StateSuccess || entry.State == StateCancelled {
			continue
		}
		if entry.TransferID != "" {
			if err := m.Cancel(entry.TransferID); err != nil && !errors.Is(err, os.ErrNotExist) {
				return err
			}
			continue
		}
		if entry.UploadID != "" {
			if upload, exists := m.store.GetUpload(entry.UploadID); exists {
				_ = os.Remove(upload.Path)
			}
			_ = m.store.DeleteUpload(entry.UploadID)
		}
		_, _ = m.store.UpdateFolderEntry(entry.ID, func(current *store.FolderEntry) error {
			if current.State != StateSuccess {
				current.State = StateCancelled
			}
			return nil
		})
	}
	m.updateBatchProgress(batchID)
	return nil
}

// RetryBatchEntry resumes a failed transfer when its local staging upload is
// still available. A client can then query the entry and continue PATCHing the
// existing upload URL without re-sending already accepted bytes.
func (m *Manager) RetryBatchEntry(batchID, entryID string) (store.FolderEntry, error) {
	entry, ok := m.store.GetFolderEntry(entryID)
	if !ok || entry.BatchID != batchID {
		return store.FolderEntry{}, os.ErrNotExist
	}
	if entry.State == StateSuccess {
		return entry, fmt.Errorf("folder entry is already %s", StateSuccess)
	}
	if entry.TransferID != "" {
		if err := m.Resume(entry.TransferID); err != nil {
			return entry, err
		}
		updated, _ := m.store.GetFolderEntry(entryID)
		return updated, nil
	}
	if entry.UploadID == "" {
		return entry, errors.New("folder entry has no resumable upload")
	}
	upload, ok := m.store.GetUpload(entry.UploadID)
	if !ok || !upload.Completed {
		return entry, errors.New("folder entry upload is incomplete")
	}
	if _, err := m.StartDriveUpload(upload); err != nil {
		return entry, err
	}
	updated, _ := m.store.GetFolderEntry(entryID)
	return updated, nil
}

// start 为任务创建独立取消上下文，并保证同一任务不会并发启动多个 worker。
func (m *Manager) start(id string) {
	m.mu.Lock()
	if m.cancels == nil {
		m.cancels = make(map[string]context.CancelFunc)
	}
	if _, exists := m.cancels[id]; exists {
		m.mu.Unlock()
		return
	}
	if m.slots == nil {
		m.slots = make(chan struct{}, 3)
	}
	slots := m.slots
	ctx, cancel := context.WithCancel(context.Background())
	m.cancels[id] = cancel
	m.mu.Unlock()
	go func() {
		defer func() {
			m.mu.Lock()
			delete(m.cancels, id)
			m.mu.Unlock()
		}()
		select {
		case slots <- struct{}{}:
			defer func() { <-slots }()
		case <-ctx.Done():
			return
		}
		t, ok := m.store.GetTransfer(id)
		if !ok {
			return
		}
		if t.State == StatePaused || t.State == StateCancelled || t.State == StateSuccess {
			return
		}
		var err error
		switch t.Kind {
		case KindDriveUpload:
			err = m.runDriveUpload(ctx, id)
		case KindPythonImport:
			err = m.runPythonImport(ctx, id)
		default:
			err = fmt.Errorf("unknown transfer kind %q", t.Kind)
		}
		if err != nil && ctx.Err() == nil {
			m.markTransferFailed(id, err)
		}
	}()
}

// cancel 查找任务对应的取消函数，并终止其当前网络或磁盘操作。
func (m *Manager) cancel(id string) {
	m.mu.Lock()
	if cancel, ok := m.cancels[id]; ok {
		cancel()
	}
	m.mu.Unlock()
}

func (m *Manager) markTransferFailed(id string, transferErr error) {
	t, ok := m.store.GetTransfer(id)
	if !ok || t.State == StateCancelled || t.State == StatePaused {
		return
	}
	updated, updateErr := m.store.UpdateTransfer(id, func(current *store.Transfer) error {
		if current.State == StateCancelled || current.State == StatePaused {
			return nil
		}
		current.State = StateFailed
		current.Error = transferErr.Error()
		return nil
	})
	if updateErr != nil || updated.State != StateFailed {
		return
	}
	if t.EntryID != "" {
		_, _ = m.store.UpdateFolderEntry(t.EntryID, func(entry *store.FolderEntry) error {
			entry.State = StateFailed
			entry.Error = transferErr.Error()
			return nil
		})
		m.updateBatchProgress(t.BatchID)
	}
}

func (m *Manager) markTransferTransferring(id string) error {
	t, ok := m.store.GetTransfer(id)
	if !ok {
		return os.ErrNotExist
	}
	if t.State == StateCancelled || t.State == StatePaused || t.State == StateSuccess {
		return nil
	}
	updated, updateErr := m.store.UpdateTransfer(id, func(current *store.Transfer) error {
		if current.State == StateCancelled || current.State == StatePaused || current.State == StateSuccess {
			return nil
		}
		current.State = StateTransferring
		return nil
	})
	if updateErr != nil || updated.State != StateTransferring {
		return updateErr
	}
	if t.EntryID != "" {
		_, _ = m.store.UpdateFolderEntry(t.EntryID, func(entry *store.FolderEntry) error {
			if entry.State == StateCancelled || entry.State == StatePaused || entry.State == StateSuccess {
				return nil
			}
			entry.State = StateTransferring
			entry.Error = ""
			return nil
		})
		m.updateBatchProgress(t.BatchID)
	}
	return nil
}

func (m *Manager) markTransferSuccess(id string, fileID string) error {
	t, ok := m.store.GetTransfer(id)
	if !ok {
		return os.ErrNotExist
	}
	if _, err := m.store.UpdateTransfer(id, func(current *store.Transfer) error {
		current.State = StateSuccess
		if fileID != "" {
			current.FileID = fileID
		}
		current.Transferred = current.TotalBytes
		return nil
	}); err != nil {
		return err
	}
	if t.EntryID != "" {
		_, _ = m.store.UpdateFolderEntry(t.EntryID, func(entry *store.FolderEntry) error {
			entry.State = StateSuccess
			entry.Error = ""
			if fileID != "" {
				entry.FileID = fileID
			}
			return nil
		})
		m.updateBatchProgress(t.BatchID)
	}
	return nil
}

// updateBatchProgress derives aggregate counters and state from durable entry
// snapshots. It is safe to call after every entry state transition.
func (m *Manager) updateBatchProgress(batchID string) {
	if batchID == "" {
		return
	}
	entries := m.store.ListFolderEntries(batchID)
	if len(entries) == 0 {
		return
	}
	completedEntries := 0
	completedBytes := int64(0)
	terminalEntries := 0
	hasFailure := false
	hasCancellation := false
	hasActive := false
	firstError := ""
	for _, entry := range entries {
		switch entry.State {
		case StateSuccess:
			completedEntries++
			completedBytes += entry.Size
			terminalEntries++
		case StateFailed:
			hasFailure = true
			if firstError == "" {
				firstError = entry.Error
			}
			terminalEntries++
		case StateCancelled:
			hasCancellation = true
			terminalEntries++
		case StateTransferring, StateRetrying, StateVerifying, StatePaused:
			hasActive = true
		}
	}
	_, _ = m.store.UpdateFolderBatch(batchID, func(batch *store.FolderBatch) error {
		batch.CompletedEntries = completedEntries
		batch.CompletedBytes = completedBytes
		batch.Error = firstError
		switch {
		case batch.TotalEntries > 0 && completedEntries == batch.TotalEntries:
			batch.State = StateSuccess
		case hasFailure:
			batch.State = StateFailed
		case batch.TotalEntries > 0 && terminalEntries == batch.TotalEntries && hasCancellation:
			batch.State = StateCancelled
		case hasActive:
			batch.State = StateTransferring
		default:
			batch.State = StatePending
		}
		return nil
	})
}

// runDriveUpload 校验本地暂存文件，通过 Drive resumable session 分片上传，并保存进度。
func (m *Manager) runDriveUpload(ctx context.Context, id string) error {
	if err := m.markTransferTransferring(id); err != nil {
		return err
	}
	t, _ := m.store.GetTransfer(id)
	file, err := os.Open(t.LocalPath)
	if err != nil {
		return fmt.Errorf("open upload staging file: %w", err)
	}
	defer file.Close()
	if t.TotalBytes == 0 {
		return errors.New("不能上传空文件")
	}
	stat, err := file.Stat()
	if err != nil {
		return err
	}
	if stat.Size() != t.TotalBytes {
		return fmt.Errorf("upload staging file size mismatch: expected=%d actual=%d", t.TotalBytes, stat.Size())
	}
	for {
		if err := ctx.Err(); err != nil {
			return err
		}
		t, ok := m.store.GetTransfer(id)
		if !ok {
			return os.ErrNotExist
		}
		if t.State == StatePaused || t.State == StateCancelled || t.State == StateSuccess {
			return nil
		}
		if t.SessionURL == "" {
			// Drive 断点会话是不透明的，可能随时过期。重置 URL 和偏移量，
			// 确保新会话不会跳过任何字节。
			if t.Transferred != 0 {
				if _, err := m.store.UpdateTransfer(id, func(t *store.Transfer) error { t.Transferred = 0; return nil }); err != nil {
					return err
				}
				t.Transferred = 0
			}
			session, sessionErr := m.drive.StartUploadSessionInFolder(ctx, t.FileName, t.MIME, t.TotalBytes, t.ParentID, t.AppProperties)
			if sessionErr != nil {
				return sessionErr
			}
			if _, err := m.store.UpdateTransfer(id, func(t *store.Transfer) error { t.SessionURL = session.URL; return nil }); err != nil {
				return err
			}
			t.SessionURL = session.URL
		}
		result, chunkErr := m.drive.UploadChunk(ctx, t.SessionURL, file, t.Transferred, t.TotalBytes, m.cfg.ChunkSizeBytes)
		if chunkErr != nil {
			if strings.Contains(chunkErr.Error(), "session expired") {
				_, _ = m.store.UpdateTransfer(id, func(t *store.Transfer) error {
					t.SessionURL = ""
					t.Transferred = 0
					t.State = StateRetrying
					return nil
				})
				continue
			}
			return chunkErr
		}
		if err := ctx.Err(); err != nil {
			return err
		}
		if result.File != nil {
			_, err = m.store.UpdateTransfer(id, func(t *store.Transfer) error {
				t.Transferred = result.NextOffset
				t.FileID = result.File.ID
				return nil
			})
		} else {
			_, err = m.store.UpdateTransferProgress(id, result.NextOffset)
		}
		if err != nil {
			return err
		}
		if result.Completed {
			if err := ctx.Err(); err != nil {
				return err
			}
			fileID := ""
			if result.File != nil {
				fileID = result.File.ID
			}
			err = m.markTransferSuccess(id, fileID)
			_ = os.Remove(t.LocalPath)
			if t.UploadID != "" {
				_ = m.store.DeleteUpload(t.UploadID)
			}
			return err
		}
	}
}

// runPythonImport 下载 Drive 文件到本地，校验 MD5 后以 multipart 形式提交 Python API。
func (m *Manager) runPythonImport(ctx context.Context, id string) error {
	if err := m.markTransferTransferring(id); err != nil {
		return err
	}
	t, _ := m.store.GetTransfer(id)
	metadata, err := m.drive.Metadata(ctx, t.FileID)
	if err != nil {
		return err
	}
	if metadata == nil {
		return errors.New("Drive metadata response is empty")
	}
	if driveclient.IsFolder(metadata) {
		return errors.New("不能把 Drive 文件夹导入 Python 流水线")
	}
	if metadata.Size <= 0 {
		return errors.New("Drive 文件为空或缺少可下载的 size 元数据")
	}
	name := filepath.Base(metadata.Name)
	if name == "." || name == string(filepath.Separator) || name == "" {
		name = "drive-import.bin"
	}
	partPath := filepath.Join(m.cfg.StagingDir(), id+"-"+name+".part")
	readyPath := strings.TrimSuffix(partPath, ".part")
	if _, err := m.store.UpdateTransfer(id, func(t *store.Transfer) error {
		t.FileName = name
		t.MIME = metadata.MimeType
		t.LocalPath = readyPath
		t.TotalBytes = metadata.Size
		t.ExpectedMD5 = metadata.Md5Checksum
		t.ETag = metadata.Etag
		return nil
	}); err != nil {
		return err
	}
	if err := os.MkdirAll(m.cfg.StagingDir(), 0o700); err != nil {
		return err
	}
	readyStat, readyErr := os.Stat(readyPath)
	if readyErr == nil && readyStat.Size() == metadata.Size {
		_, _ = m.store.UpdateTransfer(id, func(t *store.Transfer) error { t.Transferred = metadata.Size; return nil })
	} else {
		if readyErr == nil {
			_ = os.Remove(readyPath)
		}
		if err := m.downloadToFile(ctx, id, t.FileID, partPath, readyPath, metadata.Size); err != nil {
			return err
		}
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	if metadata.Md5Checksum != "" {
		_, _ = m.store.UpdateTransfer(id, func(t *store.Transfer) error { t.State = StateVerifying; return nil })
		sum, err := md5File(readyPath)
		if err != nil {
			return err
		}
		if sum != metadata.Md5Checksum {
			return fmt.Errorf("Drive 文件 MD5 校验失败：expected=%s actual=%s", metadata.Md5Checksum, sum)
		}
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	if err := m.postPythonUpload(ctx, id, readyPath, name); err != nil {
		return err
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	err = m.markTransferSuccess(id, "")
	_ = os.Remove(readyPath)
	return err
}

// downloadToFile 从已存在的 .part 文件大小恢复 Range 下载，完成后原子改名为正式文件。
func (m *Manager) downloadToFile(ctx context.Context, transferID, fileID, partPath, readyPath string, total int64) error {
	file, err := os.OpenFile(partPath, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return err
	}
	closed := false
	defer func() {
		if !closed {
			_ = file.Close()
		}
	}()
	finish := func() error {
		if err := file.Sync(); err != nil {
			return err
		}
		if err := file.Close(); err != nil {
			return err
		}
		closed = true
		return os.Rename(partPath, readyPath)
	}
	if stat, statErr := file.Stat(); statErr == nil && stat.Size() > total {
		if err := file.Truncate(0); err != nil {
			return err
		}
	}
	for {
		if err := ctx.Err(); err != nil {
			return err
		}
		stat, err := file.Stat()
		if err != nil {
			return err
		}
		offset := stat.Size()
		if offset >= total {
			if offset > total {
				if err := file.Truncate(0); err != nil {
					return err
				}
				_, _ = m.store.UpdateTransfer(transferID, func(t *store.Transfer) error { t.Transferred = 0; return nil })
				continue
			}
			return finish()
		}
		_, response, err := m.drive.DownloadRange(ctx, fileID, fmt.Sprintf("bytes=%d-", offset))
		if err != nil {
			if err := retrySleep(ctx); err != nil {
				return err
			}
			continue
		}
		if response.StatusCode != http.StatusOK && response.StatusCode != http.StatusPartialContent {
			_ = response.Body.Close()
			return fmt.Errorf("Drive range download returned %s", response.Status)
		}
		if offset > 0 && response.StatusCode == http.StatusOK {
			// 如果服务端忽略 Range，直接追加会把完整文件接到残缺文件后面。
			// 从零开始重新下载，避免本地文件静默损坏且无法校验。
			_ = response.Body.Close()
			if err := file.Truncate(0); err != nil {
				return err
			}
			_, _ = m.store.UpdateTransfer(transferID, func(t *store.Transfer) error { t.Transferred = 0; return nil })
			continue
		}
		if _, err := file.Seek(offset, io.SeekStart); err != nil {
			_ = response.Body.Close()
			return err
		}
		buf := make([]byte, 1024*1024)
		for {
			n, readErr := response.Body.Read(buf)
			if n > 0 {
				if offset+int64(n) > total {
					_ = response.Body.Close()
					return fmt.Errorf("Drive response exceeds expected size %d", total)
				}
				written, writeErr := file.Write(buf[:n])
				if writeErr != nil || written != n {
					_ = response.Body.Close()
					if writeErr != nil {
						return writeErr
					}
					return io.ErrShortWrite
				}
				newOffset := offset + int64(n)
				_, _ = m.store.UpdateTransferProgress(transferID, newOffset)
				offset = newOffset
			}
			if readErr != nil {
				_ = response.Body.Close()
				if errors.Is(readErr, io.EOF) {
					break
				}
				if err := retrySleep(ctx); err != nil {
					return err
				}
				break
			}
		}
		if offset >= total {
			return finish()
		}
	}
}

// cleanupImportFiles 删除指定导入任务产生的临时文件和已完成文件。
func (m *Manager) cleanupImportFiles(transferID string) {
	paths, err := filepath.Glob(filepath.Join(m.cfg.StagingDir(), transferID+"-*"))
	if err != nil {
		return
	}
	for _, path := range paths {
		_ = os.Remove(path)
	}
}

// postPythonUpload 流式构造 multipart 请求，将已下载文件提交到 Python 上传接口。
func (m *Manager) postPythonUpload(ctx context.Context, transferID, path, name string) error {
	if m.cfg.PythonBaseURL == "" {
		return errors.New("python_base_url 未配置")
	}
	file, err := os.Open(path)
	if err != nil {
		return err
	}
	defer file.Close()
	reader, writer := io.Pipe()
	form := multipart.NewWriter(writer)
	go func() {
		defer writer.Close()
		defer form.Close()
		_ = form.WriteField("sourceLang", "auto")
		_ = form.WriteField("targetLang", "zh-CN")
		_ = form.WriteField("mode", "mono")
		_ = form.WriteField("burn", "hard")
		_ = form.WriteField("model", "small")
		_ = form.WriteField("engine", "deepseek")
		_ = form.WriteField("needSubtitle", "true")
		part, createErr := form.CreateFormFile("file", name)
		if createErr != nil {
			_ = writer.CloseWithError(createErr)
			return
		}
		buf := make([]byte, 1024*1024)
		for {
			select {
			case <-ctx.Done():
				_ = writer.CloseWithError(ctx.Err())
				return
			default:
			}
			n, readErr := file.Read(buf)
			if n > 0 {
				if _, writeErr := part.Write(buf[:n]); writeErr != nil {
					_ = writer.CloseWithError(writeErr)
					return
				}
			}
			if readErr != nil {
				if !errors.Is(readErr, io.EOF) {
					_ = writer.CloseWithError(readErr)
				}
				return
			}
		}
	}()
	endpoint := strings.TrimRight(m.cfg.PythonBaseURL, "/") + "/api/tasks/upload"
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, reader)
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", form.FormDataContentType())
	if m.cfg.PythonAPIToken != "" {
		req.Header.Set("X-API-Token", m.cfg.PythonAPIToken)
	}
	timeout := 1800 * time.Second
	if m.cfg != nil && m.cfg.PythonTimeoutSeconds > 0 {
		timeout = time.Duration(m.cfg.PythonTimeoutSeconds) * time.Second
	}
	client := &http.Client{Timeout: timeout}
	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("调用 Python upload API: %w", err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 64*1024))
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("Python upload API 返回 %s: %s", resp.Status, strings.TrimSpace(string(body)))
	}
	var result struct {
		ID string `json:"id"`
	}
	if json.Unmarshal(body, &result) == nil && result.ID != "" {
		_, _ = m.store.UpdateTransfer(transferID, func(t *store.Transfer) error { t.PythonTaskID = result.ID; return nil })
	}
	return nil
}

// md5File 流式计算文件的 MD5，用于与 Drive 元数据进行完整性校验。
func md5File(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := md5.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// retrySleep 在下载失败后等待固定间隔，并响应任务取消。
func retrySleep(ctx context.Context) error {
	t := time.NewTimer(2 * time.Second)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-t.C:
		return nil
	}
}
