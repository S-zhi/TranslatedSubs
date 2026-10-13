// Package httpapi 对外提供 Web 客户端使用的本地 HTTP 接口，并将传输层职责与
// OAuth、Drive 客户端和后台传输 worker 分离。
package httpapi

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	"github.com/s-zhi/subtitles-ai-drive/internal/config"
	driveclient "github.com/s-zhi/subtitles-ai-drive/internal/drive"
	"github.com/s-zhi/subtitles-ai-drive/internal/oauth"
	"github.com/s-zhi/subtitles-ai-drive/internal/store"
	"github.com/s-zhi/subtitles-ai-drive/internal/transfer"
)

// Server 将 OAuth、文件、上传和传输请求路由到进程内共享的服务实例。
type Server struct {
	cfg       *config.Config
	auth      *oauth.Manager
	drive     DriveClient
	transfers *transfer.Manager
	store     *store.Store

	uploadLocksMu sync.Mutex
	uploadLocks   map[string]*uploadLock
}

// uploadLock tracks active users of a per-upload mutex so idle entries can be removed.
type uploadLock struct {
	mu   sync.Mutex
	refs int
}

// DriveClient is the HTTP surface needed by the API layer. The production
// drive.Client satisfies it; the interface also lets route tests exercise
// folder-tree ordering without contacting Google.
type DriveClient interface {
	List(context.Context, string, string, int64) (*driveclient.FileList, error)
	CreateFolder(context.Context, string, string, map[string]string) (*driveclient.File, error)
	ValidateFolderUnderRoot(context.Context, string) (*driveclient.File, error)
	Metadata(context.Context, string) (*driveclient.File, error)
	Trash(context.Context, string) error
	DownloadRange(context.Context, string, string) (*driveclient.File, *http.Response, error)
}

// folderManifestItem is intentionally tolerant of the two names commonly
// emitted by browser clients (path and relativePath). The server normalizes
// both into a single safe relative path before touching Drive.
type folderManifestItem struct {
	Path              string `json:"path"`
	RelativePath      string `json:"relativePath"`
	RelativePathSnake string `json:"relative_path"`
	Name              string `json:"name"`
	MIME              string `json:"mime"`
	MimeType          string `json:"mimeType"`
	MimeTypeSnake     string `json:"mime_type"`
	Size              int64  `json:"size"`
}

type folderUploadRequest struct {
	Entries              []folderManifestItem `json:"entries"`
	Files                []folderManifestItem `json:"files"`
	ParentID             string               `json:"parentId"`
	ParentIDSnake        string               `json:"parent_id"`
	ClientRequestID      string               `json:"clientRequestId"`
	ClientRequestIDSnake string               `json:"client_request_id"`
}

type folderUploadResponse struct {
	Batch   store.FolderBatch   `json:"batch"`
	Entries []store.FolderEntry `json:"entries"`
}

// taskFolderRequest deliberately accepts only the unique task ID.  The Drive
// folder name is derived from it so callers cannot accidentally key a folder
// by a mutable/non-unique task title.
type taskFolderRequest struct {
	TaskID string `json:"taskId"`
}

var errFolderEntryUploadExists = errors.New("folder entry already has an upload")

// New 将 HTTP 外观层与单用户服务依赖连接起来。
func New(cfg *config.Config, auth *oauth.Manager, client DriveClient, transfers *transfer.Manager, state *store.Store) *Server {
	return &Server{
		cfg:         cfg,
		auth:        auth,
		drive:       client,
		transfers:   transfers,
		store:       state,
		uploadLocks: make(map[string]*uploadLock),
	}
}

// lockUpload serializes file and metadata operations for one upload ID while
// allowing unrelated uploads to proceed concurrently.
func (s *Server) lockUpload(id string) func() {
	s.uploadLocksMu.Lock()
	lock := s.uploadLocks[id]
	if lock == nil {
		lock = &uploadLock{}
		s.uploadLocks[id] = lock
	}
	lock.refs++
	s.uploadLocksMu.Unlock()

	lock.mu.Lock()
	return func() {
		lock.mu.Unlock()
		s.uploadLocksMu.Lock()
		lock.refs--
		if lock.refs == 0 {
			delete(s.uploadLocks, id)
		}
		s.uploadLocksMu.Unlock()
	}
}

// ServeHTTP 实现完整的本地 API。长时间运行的上传/下载会交给 transfer.Manager，
// 因此请求取消不会抹掉已持久化的断点。
func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	s.setCommonHeaders(w, r)
	if r.Method == http.MethodOptions {
		w.WriteHeader(http.StatusNoContent)
		return
	}
	if r.URL.Path == "/healthz" {
		writeJSON(w, http.StatusOK, map[string]any{"ok": true})
		return
	}
	if r.URL.Path == "/api/oauth/status" && r.Method == http.MethodGet {
		writeJSON(w, http.StatusOK, s.auth.Status())
		return
	}
	if r.URL.Path == "/api/oauth/google/start" && r.Method == http.MethodGet {
		url, err := s.auth.Start()
		if err != nil {
			writeError(w, http.StatusServiceUnavailable, err)
			return
		}
		http.Redirect(w, r, url, http.StatusFound)
		return
	}
	if r.URL.Path == "/api/oauth/google/disconnect" && (r.Method == http.MethodPost || r.Method == http.MethodDelete) {
		if err := s.auth.Disconnect(r.Context()); err != nil {
			writeError(w, http.StatusBadGateway, err)
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"connected": false})
		return
	}

	if r.URL.Path == "/api/drive/files" && r.Method == http.MethodGet {
		s.handleListFiles(w, r)
		return
	}
	if r.URL.Path == "/api/drive/folder-uploads" && r.Method == http.MethodPost {
		s.handleCreateFolderUpload(w, r)
		return
	}
	if r.URL.Path == "/api/drive/task-folders" && r.Method == http.MethodPost {
		s.handleCreateTaskFolder(w, r)
		return
	}
	if r.URL.Path == "/api/drive/uploads" && r.Method == http.MethodPost {
		s.handleCreateUpload(w, r)
		return
	}
	if r.URL.Path == "/api/drive/transfers" && r.Method == http.MethodGet {
		writeJSON(w, http.StatusOK, map[string]any{"transfers": s.store.ListTransfers()})
		return
	}

	parts := splitPath(r.URL.Path)
	if len(parts) == 3 && parts[0] == "api" && parts[1] == "drive" && parts[2] == "folder-uploads" && r.Method == http.MethodPost {
		s.handleCreateFolderUpload(w, r)
		return
	}
	if len(parts) >= 4 && parts[0] == "api" && parts[1] == "drive" && parts[2] == "folder-uploads" {
		s.handleFolderUploadRoute(w, r, parts[3:])
		return
	}
	if len(parts) >= 4 && parts[0] == "api" && parts[1] == "drive" && parts[2] == "uploads" {
		if len(parts) == 4 {
			switch r.Method {
			case http.MethodHead, http.MethodPatch:
				s.handleUploadChunk(w, r, parts[3])
				return
			case http.MethodDelete:
				s.handleDeleteUpload(w, parts[3])
				return
			}
		}
	}
	if len(parts) >= 4 && parts[0] == "api" && parts[1] == "drive" && parts[2] == "transfers" {
		if len(parts) == 4 && r.Method == http.MethodGet {
			s.handleTransferGet(w, parts[3])
			return
		}
		if len(parts) == 5 && r.Method == http.MethodPost {
			s.handleTransferAction(w, parts[3], parts[4])
			return
		}
	}
	if len(parts) >= 4 && parts[0] == "api" && parts[1] == "drive" && parts[2] == "files" {
		fileID := parts[3]
		if len(parts) == 4 && r.Method == http.MethodDelete {
			s.handleDeleteFile(w, r, fileID)
			return
		}
		if len(parts) == 5 && parts[4] == "download" && (r.Method == http.MethodGet || r.Method == http.MethodHead) {
			s.handleDownload(w, r, fileID)
			return
		}
		if len(parts) == 5 && parts[4] == "import" && r.Method == http.MethodPost {
			s.handleImport(w, r, fileID)
			return
		}
	}
	writeError(w, http.StatusNotFound, errors.New("route not found"))
}

// handleCreateTaskFolder gets or creates the task folder below the sidecar
// root.  Lookup is by appProperties.subtitles_ai_task_id, never by the
// display name, so a retry cannot create a second folder for the same task.
func (s *Server) handleCreateTaskFolder(w http.ResponseWriter, r *http.Request) {
	var request taskFolderRequest
	decoder := json.NewDecoder(io.LimitReader(r.Body, 64*1024))
	if err := decoder.Decode(&request); err != nil {
		writeError(w, http.StatusBadRequest, fmt.Errorf("解析 task folder 请求失败: %w", err))
		return
	}
	taskID := strings.TrimSpace(request.TaskID)
	if taskID == "" || filepath.Base(taskID) != taskID || strings.ContainsAny(taskID, "\\/\x00") {
		writeError(w, http.StatusBadRequest, errors.New("taskId 必须是非空的安全唯一 ID"))
		return
	}

	rootFiles, err := s.drive.List(r.Context(), "", "", 1000)
	if err != nil {
		writeDriveError(w, err)
		return
	}
	var found *driveclient.File
	for index := range rootFiles.Files {
		file := rootFiles.Files[index]
		if !driveclient.IsFolder(&file) || file.Trashed {
			continue
		}
		if file.AppProperties["subtitles_ai_task_id"] != taskID {
			continue
		}
		if found != nil {
			writeError(w, http.StatusConflict, fmt.Errorf("taskId %q 对应多个 Drive 文件夹", taskID))
			return
		}
		copy := file
		found = &copy
	}
	if found != nil {
		writeJSON(w, http.StatusOK, map[string]any{"folder": found, "taskId": taskID, "created": false})
		return
	}

	folder, err := s.drive.CreateFolder(r.Context(), taskID, "", map[string]string{
		"subtitles_ai_task_id": taskID,
		"subtitles_ai_kind":    "task_artifacts",
	})
	if err != nil {
		writeDriveError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{"folder": folder, "taskId": taskID, "created": true})
}

// handleFolderUploadRoute dispatches status and entry actions for a batch.
func (s *Server) handleFolderUploadRoute(w http.ResponseWriter, r *http.Request, parts []string) {
	if len(parts) == 1 && r.Method == http.MethodGet {
		s.handleFolderUploadStatus(w, r, parts[0])
		return
	}
	if len(parts) == 2 && r.Method == http.MethodPost {
		switch parts[1] {
		case "retry":
			s.handleFolderUploadRetry(w, r, parts[0])
			return
		case "cancel":
			s.handleFolderUploadCancel(w, r, parts[0])
			return
		}
	}
	if len(parts) == 2 && parts[1] == "cancel" && r.Method == http.MethodDelete {
		s.handleFolderUploadCancel(w, r, parts[0])
		return
	}
	if len(parts) == 4 && parts[1] == "entries" && r.Method == http.MethodPost {
		switch parts[3] {
		case "upload":
			s.handleFolderEntryUpload(w, r, parts[0], parts[2])
			return
		case "retry":
			s.handleFolderEntryRetry(w, r, parts[0], parts[2])
			return
		}
	}
	if len(parts) == 3 && parts[1] == "entries" && r.Method == http.MethodPost {
		s.handleFolderEntryUpload(w, r, parts[0], parts[2])
		return
	}
	if len(parts) == 3 && parts[1] == "entries" && r.Method == http.MethodGet {
		s.handleFolderEntryStatus(w, parts[0], parts[2])
		return
	}
	writeError(w, http.StatusNotFound, errors.New("folder upload route not found"))
}

func (s *Server) handleFolderEntryStatus(w http.ResponseWriter, batchID, entryID string) {
	entry, ok := s.store.GetFolderEntry(entryID)
	if !ok || entry.BatchID != batchID {
		writeError(w, http.StatusNotFound, errors.New("folder entry not found"))
		return
	}
	writeJSON(w, http.StatusOK, entry)
}

func (s *Server) handleFolderUploadStatus(w http.ResponseWriter, _ *http.Request, batchID string) {
	batch, ok := s.store.GetFolderBatch(batchID)
	if !ok {
		writeError(w, http.StatusNotFound, errors.New("folder upload not found"))
		return
	}
	writeJSON(w, http.StatusOK, folderUploadResponse{Batch: batch, Entries: s.store.ListFolderEntries(batchID)})
}

func (s *Server) handleFolderUploadRetry(w http.ResponseWriter, _ *http.Request, batchID string) {
	batch, ok := s.store.GetFolderBatch(batchID)
	if !ok {
		writeError(w, http.StatusNotFound, errors.New("folder upload not found"))
		return
	}
	var firstErr error
	for _, entry := range s.store.ListFolderEntries(batchID) {
		if entry.State != transfer.StateFailed && entry.State != transfer.StatePaused {
			continue
		}
		if _, err := s.transfers.RetryBatchEntry(batchID, entry.ID); err != nil && firstErr == nil {
			firstErr = err
		}
	}
	if firstErr != nil {
		writeError(w, http.StatusConflict, firstErr)
		return
	}
	batch, _ = s.store.GetFolderBatch(batchID)
	writeJSON(w, http.StatusAccepted, folderUploadResponse{Batch: batch, Entries: s.store.ListFolderEntries(batchID)})
}

func (s *Server) handleFolderUploadCancel(w http.ResponseWriter, _ *http.Request, batchID string) {
	if err := s.transfers.CancelBatch(batchID); err != nil {
		if errors.Is(err, os.ErrNotExist) {
			writeError(w, http.StatusNotFound, err)
			return
		}
		writeError(w, http.StatusConflict, err)
		return
	}
	batch, _ := s.store.GetFolderBatch(batchID)
	writeJSON(w, http.StatusOK, folderUploadResponse{Batch: batch, Entries: s.store.ListFolderEntries(batchID)})
}

func (s *Server) handleFolderEntryRetry(w http.ResponseWriter, _ *http.Request, batchID, entryID string) {
	entry, err := s.transfers.RetryBatchEntry(batchID, entryID)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			writeError(w, http.StatusNotFound, err)
			return
		}
		writeError(w, http.StatusConflict, err)
		return
	}
	batch, _ := s.store.GetFolderBatch(batchID)
	writeJSON(w, http.StatusAccepted, folderUploadResponse{Batch: batch, Entries: []store.FolderEntry{entry}})
}

// handleCreateFolderUpload validates and persists a manifest, creates all
// required Drive folders in parent-before-child order, and returns durable
// entry IDs for subsequent resumable uploads.
func (s *Server) handleCreateFolderUpload(w http.ResponseWriter, r *http.Request) {
	request, err := s.decodeFolderManifest(w, r)
	if err != nil {
		return
	}
	maxEntries := 1000
	maxBytes := int64(2 * 1024 * 1024 * 1024)
	maxFileBytes := maxBytes
	maxDepth := 64
	maxDirectories := 5000
	if s.cfg != nil {
		if s.cfg.MaxFolderEntries > 0 {
			maxEntries = s.cfg.MaxFolderEntries
		}
		if s.cfg.MaxFolderBytes > 0 {
			maxBytes = s.cfg.MaxFolderBytes
		}
		if s.cfg.MaxUploadBytes > 0 {
			maxFileBytes = s.cfg.MaxUploadBytes
		}
		if s.cfg.MaxFolderDepth > 0 {
			maxDepth = s.cfg.MaxFolderDepth
		}
		if s.cfg.MaxFolderDirectories > 0 {
			maxDirectories = s.cfg.MaxFolderDirectories
		}
	}
	items, err := normalizeFolderManifestWithDepth(request.Entries, maxEntries, maxBytes, maxFileBytes, maxDepth, maxDirectories)
	if err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	clientRequestID := firstNonEmpty(
		r.Header.Get("X-Client-Request-ID"),
		r.Header.Get("Idempotency-Key"),
		request.ClientRequestID,
		request.ClientRequestIDSnake,
	)
	parentID := firstNonEmpty(r.URL.Query().Get("parentId"), r.URL.Query().Get("parent_id"), request.ParentID, request.ParentIDSnake)
	if existing, ok := s.store.GetFolderBatchByClientRequestID(clientRequestID); ok {
		writeJSON(w, http.StatusOK, folderUploadResponse{Batch: existing, Entries: s.store.ListFolderEntries(existing.ID)})
		return
	}
	if parentID != "" {
		if _, err := s.drive.ValidateFolderUnderRoot(r.Context(), parentID); err != nil {
			writeDriveError(w, err)
			return
		}
	}
	totalBytes := int64(0)
	for _, item := range items {
		totalBytes += item.Size
	}
	batch, existing, err := s.store.CreateFolderBatch(clientRequestID, parentID, len(items), totalBytes)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	if existing {
		writeJSON(w, http.StatusOK, folderUploadResponse{Batch: batch, Entries: s.store.ListFolderEntries(batch.ID)})
		return
	}
	folders, paths, err := s.createFolderTree(r.Context(), batch.ID, parentID, items)
	if err != nil {
		s.cleanupCreatedFolders(r.Context(), folders, paths)
		_, _ = s.store.UpdateFolderBatch(batch.ID, func(current *store.FolderBatch) error {
			current.State = transfer.StateFailed
			current.Error = err.Error()
			return nil
		})
		writeDriveError(w, err)
		return
	}
	for _, item := range items {
		dir := folderDir(item.Path)
		entryParent := parentID
		if dir != "" {
			entryParent = folders[dir]
		}
		entry, createErr := s.store.CreateFolderEntry(store.FolderEntry{
			BatchID: batch.ID, RelativePath: item.Path, Name: item.Name,
			MIME: item.MIME, Size: item.Size, ParentID: entryParent,
			FolderID: entryParent, State: transfer.StatePending,
		})
		if createErr != nil {
			s.cleanupCreatedFolders(r.Context(), folders, paths)
			_, _ = s.store.UpdateFolderBatch(batch.ID, func(current *store.FolderBatch) error {
				current.State = transfer.StateFailed
				current.Error = createErr.Error()
				return nil
			})
			writeError(w, http.StatusInternalServerError, createErr)
			return
		}
		_ = entry
	}
	writeJSON(w, http.StatusCreated, folderUploadResponse{Batch: batch, Entries: s.store.ListFolderEntries(batch.ID)})
}

func (s *Server) decodeFolderManifest(w http.ResponseWriter, r *http.Request) (folderUploadRequest, error) {
	limit := int64(8 * 1024 * 1024)
	if s.cfg != nil && s.cfg.MaxManifestBytes > 0 {
		limit = s.cfg.MaxManifestBytes
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, limit+1))
	if err != nil {
		writeError(w, http.StatusBadRequest, fmt.Errorf("读取 manifest 失败: %w", err))
		return folderUploadRequest{}, err
	}
	if int64(len(body)) > limit {
		err = fmt.Errorf("manifest 超过最大大小 %d bytes", limit)
		writeError(w, http.StatusRequestEntityTooLarge, err)
		return folderUploadRequest{}, err
	}
	var request folderUploadRequest
	if len(strings.TrimSpace(string(body))) == 0 {
		err = errors.New("manifest 不能为空")
		writeError(w, http.StatusBadRequest, err)
		return folderUploadRequest{}, err
	}
	trimmed := strings.TrimSpace(string(body))
	if strings.HasPrefix(trimmed, "[") {
		if err = json.Unmarshal(body, &request.Entries); err != nil {
			err = fmt.Errorf("解析 manifest 失败: %w", err)
			writeError(w, http.StatusBadRequest, err)
			return folderUploadRequest{}, err
		}
	} else if err = json.Unmarshal(body, &request); err != nil {
		err = fmt.Errorf("解析 manifest 失败: %w", err)
		writeError(w, http.StatusBadRequest, err)
		return folderUploadRequest{}, err
	}
	if len(request.Entries) == 0 {
		request.Entries = request.Files
	}
	return request, nil
}

func normalizeFolderManifest(items []folderManifestItem) ([]folderManifestItem, error) {
	return normalizeFolderManifestWithDepth(items, 1000, 2*1024*1024*1024, 2*1024*1024*1024, 64, 5000)
}

func normalizeFolderManifestWithLimits(items []folderManifestItem, maxEntries int, maxBytes, maxFileBytes int64) ([]folderManifestItem, error) {
	return normalizeFolderManifestWithDepth(items, maxEntries, maxBytes, maxFileBytes, 64, 5000)
}

func normalizeFolderManifestWithDepth(items []folderManifestItem, maxEntries int, maxBytes, maxFileBytes int64, maxDepth, maxDirectories int) ([]folderManifestItem, error) {
	if len(items) == 0 {
		return nil, errors.New("manifest 至少需要一个文件")
	}
	if maxEntries <= 0 {
		maxEntries = 1000
	}
	if maxBytes <= 0 {
		maxBytes = 2 * 1024 * 1024 * 1024
	}
	if maxFileBytes <= 0 {
		maxFileBytes = maxBytes
	}
	if maxDepth <= 0 {
		maxDepth = 64
	}
	if maxDirectories <= 0 {
		maxDirectories = 5000
	}
	if len(items) > maxEntries {
		return nil, fmt.Errorf("manifest 文件数量超过上限 %d", maxEntries)
	}
	result := make([]folderManifestItem, 0, len(items))
	seen := make(map[string]struct{}, len(items))
	directories := make(map[string]struct{})
	totalBytes := int64(0)
	for _, item := range items {
		raw := firstNonEmpty(item.RelativePath, item.RelativePathSnake, item.Path, item.Name)
		normalized, err := normalizeRelativePath(raw)
		if err != nil {
			return nil, err
		}
		parts := strings.Split(normalized, "/")
		if len(parts)-1 > maxDepth {
			return nil, fmt.Errorf("manifest 路径深度超过上限 %d", maxDepth)
		}
		for depth := 1; depth < len(parts); depth++ {
			directories[strings.Join(parts[:depth], "/")] = struct{}{}
		}
		if len(directories) > maxDirectories {
			return nil, fmt.Errorf("manifest 目录数量超过上限 %d", maxDirectories)
		}
		key := strings.ToLower(normalized)
		if _, exists := seen[key]; exists {
			return nil, fmt.Errorf("manifest 包含重复路径 %q", normalized)
		}
		seen[key] = struct{}{}
		if item.Size <= 0 {
			return nil, fmt.Errorf("文件 %q 的 size 必须是正整数", normalized)
		}
		if item.Size > maxFileBytes {
			return nil, fmt.Errorf("文件 %q 超过最大大小 %d bytes", normalized, maxFileBytes)
		}
		if item.Size > maxBytes {
			return nil, fmt.Errorf("文件 %q 使 manifest 总大小超过上限 %d bytes", normalized, maxBytes)
		}
		if totalBytes > maxBytes-item.Size {
			return nil, errors.New("manifest 总大小超过上限")
		}
		totalBytes += item.Size
		mime := firstNonEmpty(item.MIME, item.MimeType, item.MimeTypeSnake, "application/octet-stream")
		if strings.EqualFold(strings.TrimSpace(strings.SplitN(mime, ";", 2)[0]), "application/vnd.google-apps.folder") {
			return nil, fmt.Errorf("manifest 不允许包含文件夹 %q", normalized)
		}
		item.Path = normalized
		item.Name = path.Base(normalized)
		item.MIME = mime
		result = append(result, item)
	}
	return result, nil
}

func normalizeRelativePath(raw string) (string, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return "", errors.New("manifest 路径不能为空")
	}
	if len(raw) > 4096 || strings.ContainsRune(raw, '\x00') {
		return "", errors.New("manifest 路径过长或包含 NUL 字符")
	}
	raw = strings.ReplaceAll(raw, "\\", "/")
	if strings.HasPrefix(raw, "/") || strings.HasPrefix(raw, "~") {
		return "", fmt.Errorf("manifest 路径必须是相对路径: %q", raw)
	}
	parts := strings.Split(raw, "/")
	if len(parts) == 0 {
		return "", errors.New("manifest 路径不能为空")
	}
	for index, part := range parts {
		if part == "" || part == "." || part == ".." {
			return "", fmt.Errorf("manifest 路径包含不安全片段: %q", raw)
		}
		if index == 0 && strings.Contains(part, ":") {
			return "", fmt.Errorf("manifest 路径包含 Windows 盘符: %q", raw)
		}
		for _, r := range part {
			if r < 0x20 || r == 0x7f {
				return "", fmt.Errorf("manifest 路径包含控制字符: %q", raw)
			}
		}
	}
	return path.Clean(strings.Join(parts, "/")), nil
}

func (s *Server) createFolderTree(ctx context.Context, batchID, rootParentID string, items []folderManifestItem) (map[string]string, []string, error) {
	directories := make(map[string]struct{})
	for _, item := range items {
		parts := strings.Split(item.Path, "/")
		for depth := 1; depth < len(parts); depth++ {
			directories[strings.Join(parts[:depth], "/")] = struct{}{}
		}
	}
	paths := make([]string, 0, len(directories))
	for directory := range directories {
		paths = append(paths, directory)
	}
	sort.Slice(paths, func(i, j int) bool {
		leftDepth, rightDepth := strings.Count(paths[i], "/"), strings.Count(paths[j], "/")
		if leftDepth == rightDepth {
			return paths[i] < paths[j]
		}
		return leftDepth < rightDepth
	})
	folders := make(map[string]string, len(paths))
	for _, directory := range paths {
		parts := strings.Split(directory, "/")
		parentID := rootParentID
		if len(parts) > 1 {
			parentID = folders[strings.Join(parts[:len(parts)-1], "/")]
		}
		folder, err := s.drive.CreateFolder(ctx, parts[len(parts)-1], parentID, map[string]string{
			"subtitles_ai_batch_id":      batchID,
			"subtitles_ai_relative_path": directory,
		})
		if err != nil {
			return folders, paths, fmt.Errorf("创建目录 %q 失败: %w", directory, err)
		}
		if folder == nil || folder.ID == "" {
			return folders, paths, fmt.Errorf("创建目录 %q 返回空 ID", directory)
		}
		folders[directory] = folder.ID
	}
	return folders, paths, nil
}

func (s *Server) cleanupCreatedFolders(ctx context.Context, folders map[string]string, paths []string) {
	if len(folders) == 0 {
		return
	}
	cleanupCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 30*time.Second)
	defer cancel()

	for i := len(paths) - 1; i >= 0; i-- {
		dir := paths[i]
		folderID, ok := folders[dir]
		if !ok || folderID == "" {
			continue
		}
		_ = s.drive.Trash(cleanupCtx, folderID)
	}
}

func folderDir(relativePath string) string {
	dir := path.Dir(relativePath)
	if dir == "." {
		return ""
	}
	return dir
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return strings.TrimSpace(value)
		}
	}
	return ""
}

// uploadFileNameHeader decodes the browser-safe UTF-8 filename header while
// preserving compatibility with clients that still send an ASCII filename in
// X-File-Name. The boolean reports whether either header was supplied.
func uploadFileNameHeader(r *http.Request) (string, bool, error) {
	if encoded := r.Header.Get("X-File-Name-Encoded"); encoded != "" {
		name, err := url.PathUnescape(encoded)
		if err != nil {
			return "", true, fmt.Errorf("X-File-Name-Encoded 编码无效: %w", err)
		}
		if !utf8.ValidString(name) {
			return "", true, errors.New("X-File-Name-Encoded 不是有效 UTF-8")
		}
		for _, value := range name {
			if value < 0x20 || value == 0x7f {
				return "", true, errors.New("文件名不能包含控制字符")
			}
		}
		if name == "" {
			return "", true, errors.New("文件名不能为空")
		}
		return name, true, nil
	}
	name := r.Header.Get("X-File-Name")
	return name, name != "", nil
}

// handleFolderEntryUpload creates the normal local resumable upload record,
// but attaches durable batch/entry/parent metadata before PATCH begins.
func (s *Server) handleFolderEntryUpload(w http.ResponseWriter, r *http.Request, batchID, entryID string) {
	batch, ok := s.store.GetFolderBatch(batchID)
	if !ok {
		writeError(w, http.StatusNotFound, errors.New("folder upload not found"))
		return
	}
	entry, ok := s.store.GetFolderEntry(entryID)
	if !ok || entry.BatchID != batchID {
		writeError(w, http.StatusNotFound, errors.New("folder entry not found"))
		return
	}
	if batch.State == transfer.StateCancelled || batch.State == transfer.StateSuccess {
		writeError(w, http.StatusConflict, fmt.Errorf("folder upload is already %s", batch.State))
		return
	}
	if entry.State == transfer.StateSuccess {
		writeError(w, http.StatusConflict, errors.New("folder entry is already SUCCESS"))
		return
	}
	lengthHeader := firstNonEmpty(r.Header.Get("X-Upload-Length"), r.Header.Get("Upload-Length"))
	length, err := strconv.ParseInt(lengthHeader, 10, 64)
	if err != nil || length <= 0 {
		writeError(w, http.StatusBadRequest, errors.New("X-Upload-Length 必须是正整数"))
		return
	}
	if length != entry.Size {
		writeError(w, http.StatusConflict, fmt.Errorf("entry size mismatch, manifest=%d request=%d", entry.Size, length))
		return
	}
	maxUpload := int64(2 * 1024 * 1024 * 1024)
	if s.cfg != nil && s.cfg.MaxUploadBytes > 0 {
		maxUpload = s.cfg.MaxUploadBytes
	}
	if length > maxUpload {
		writeError(w, http.StatusRequestEntityTooLarge, fmt.Errorf("文件超过最大大小 %d bytes", maxUpload))
		return
	}
	if entry.UploadID != "" {
		if upload, exists := s.store.GetUpload(entry.UploadID); exists {
			writeUploadCreated(w, upload, http.StatusOK)
			return
		}
	}
	name := entry.Name
	requestedName, nameProvided, err := uploadFileNameHeader(r)
	if err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	if nameProvided {
		requestedName = path.Base(strings.ReplaceAll(requestedName, "\\", "/"))
		if requestedName != name {
			writeError(w, http.StatusConflict, fmt.Errorf("entry name mismatch, manifest=%q request=%q", name, requestedName))
			return
		}
	}
	mime := firstNonEmpty(entry.MIME, "application/octet-stream")
	if requestedMIME := r.Header.Get("X-File-Mime"); requestedMIME != "" && !strings.EqualFold(requestedMIME, mime) {
		writeError(w, http.StatusConflict, fmt.Errorf("entry mime mismatch, manifest=%q request=%q", mime, requestedMIME))
		return
	}
	if err := os.MkdirAll(s.cfg.StagingDir(), 0o700); err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	stagingPath := filepath.Join(s.cfg.StagingDir(), fmt.Sprintf("folder-upload-%d.part", time.Now().UnixNano()))
	file, err := os.OpenFile(stagingPath, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	if err := file.Close(); err != nil {
		_ = os.Remove(stagingPath)
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	upload, err := s.store.CreateUploadWithMetadata(stagingPath, name, mime, length, batchID, entryID, entry.ParentID, entry.RelativePath)
	if err != nil {
		_ = os.Remove(stagingPath)
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	if _, err := s.store.UpdateFolderEntry(entryID, func(current *store.FolderEntry) error {
		if current.UploadID != "" {
			return errFolderEntryUploadExists
		}
		current.UploadID = upload.ID
		current.State = transfer.StatePending
		current.Error = ""
		return nil
	}); err != nil {
		_ = os.Remove(stagingPath)
		_ = s.store.DeleteUpload(upload.ID)
		if errors.Is(err, errFolderEntryUploadExists) {
			if existingEntry, exists := s.store.GetFolderEntry(entryID); exists && existingEntry.UploadID != "" {
				if existingUpload, exists := s.store.GetUpload(existingEntry.UploadID); exists {
					writeUploadCreated(w, existingUpload, http.StatusOK)
					return
				}
			}
		}
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	writeUploadCreated(w, upload, http.StatusCreated)
}

func writeUploadCreated(w http.ResponseWriter, upload store.Upload, status int) {
	w.Header().Set("Location", "/api/drive/uploads/"+upload.ID)
	w.Header().Set("Upload-Offset", strconv.FormatInt(upload.Offset, 10))
	w.Header().Set("Upload-Length", strconv.FormatInt(upload.Length, 10))
	writeJSON(w, status, map[string]any{
		"id": upload.ID, "uploadUrl": "/api/drive/uploads/" + upload.ID,
		"offset": upload.Offset, "length": upload.Length, "name": upload.Name,
		"batchId": upload.BatchID, "entryId": upload.EntryID,
	})
}

// handleListFiles 解析分页参数并返回 Drive 应用目录中的文件列表。
func (s *Server) handleListFiles(w http.ResponseWriter, r *http.Request) {
	pageSize, _ := strconv.ParseInt(r.URL.Query().Get("pageSize"), 10, 64)
	files, err := s.drive.List(r.Context(), r.URL.Query().Get("parentId"), r.URL.Query().Get("pageToken"), pageSize)
	if err != nil {
		writeDriveError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, files)
}

// handleDeleteFile 将指定 Drive 文件移入回收站并返回空响应。
func (s *Server) handleDeleteFile(w http.ResponseWriter, r *http.Request, fileID string) {
	if err := s.drive.Trash(r.Context(), fileID); err != nil {
		writeDriveError(w, err)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

// handleDownload 将 Drive 媒体响应转发给客户端，并保留 Range 相关响应头。
func (s *Server) handleDownload(w http.ResponseWriter, r *http.Request, fileID string) {
	metadata, err := s.drive.Metadata(r.Context(), fileID)
	if err != nil {
		writeDriveError(w, err)
		return
	}
	if metadata == nil {
		writeDriveError(w, errors.New("Drive metadata response is empty"))
		return
	}
	if driveclient.IsFolder(metadata) {
		writeError(w, http.StatusBadRequest, errors.New("不能下载 Drive 文件夹"))
		return
	}
	_, response, err := s.drive.DownloadRange(r.Context(), fileID, r.Header.Get("Range"))
	if err != nil {
		writeDriveError(w, err)
		return
	}
	defer response.Body.Close()
	copyHeader(w.Header(), response.Header, "Content-Range", "Content-Length", "Accept-Ranges", "ETag", "Last-Modified")
	if metadata.MimeType != "" {
		w.Header().Set("Content-Type", metadata.MimeType)
	}
	name := filepath.Base(metadata.Name)
	if name != "" && name != "." {
		w.Header().Set("Content-Disposition", fmt.Sprintf("attachment; filename=%q", name))
	}
	w.WriteHeader(response.StatusCode)
	if r.Method == http.MethodHead {
		return
	}
	_, _ = io.Copy(w, response.Body)
}

// handleImport 创建一个异步 Drive 到 Python 流水线的导入任务。
func (s *Server) handleImport(w http.ResponseWriter, r *http.Request, fileID string) {
	t, err := s.transfers.StartPythonImport(fileID)
	if err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	writeJSON(w, http.StatusAccepted, t)
}

// handleCreateUpload 创建本地暂存上传记录，并返回后续分片上传地址。
func (s *Server) handleCreateUpload(w http.ResponseWriter, r *http.Request) {
	// 浏览器先将文件上传到本地磁盘，使请求协议不受 Drive 网络延迟影响，
	// 并允许后台 worker 在重启后继续任务。
	lengthHeader := r.Header.Get("X-Upload-Length")
	if lengthHeader == "" {
		lengthHeader = r.Header.Get("Upload-Length")
	}
	length, err := strconv.ParseInt(lengthHeader, 10, 64)
	if err != nil || length <= 0 {
		writeError(w, http.StatusBadRequest, errors.New("X-Upload-Length 必须是正整数"))
		return
	}
	if length > s.cfg.MaxUploadBytes {
		writeError(w, http.StatusRequestEntityTooLarge, fmt.Errorf("文件超过最大大小 %d bytes", s.cfg.MaxUploadBytes))
		return
	}
	if err := os.MkdirAll(s.cfg.StagingDir(), 0o700); err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	name, nameProvided, err := uploadFileNameHeader(r)
	if err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	if !nameProvided {
		name = "upload.bin"
	}
	mime := r.Header.Get("X-File-Mime")
	if mime == "" {
		mime = "application/octet-stream"
	}
	finalPath := filepath.Join(s.cfg.StagingDir(), fmt.Sprintf("upload-%d.part", time.Now().UnixNano()))
	file, err := os.OpenFile(finalPath, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	if err := file.Close(); err != nil {
		_ = os.Remove(finalPath)
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	u, err := s.store.CreateUpload(finalPath, filepath.Base(name), mime, length)
	if err != nil {
		_ = os.Remove(finalPath)
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	w.Header().Set("Location", "/api/drive/uploads/"+u.ID)
	w.Header().Set("Upload-Offset", "0")
	w.Header().Set("Upload-Length", strconv.FormatInt(u.Length, 10))
	writeJSON(w, http.StatusCreated, map[string]any{
		"id": u.ID, "uploadUrl": "/api/drive/uploads/" + u.ID,
		"offset": u.Offset, "length": u.Length, "name": u.Name,
	})
}

// handleUploadChunk 处理 HEAD 进度查询和 PATCH 分片写入，并在完成后排队 Drive 上传。
func (s *Server) handleUploadChunk(w http.ResponseWriter, r *http.Request, id string) {
	unlock := s.lockUpload(id)
	defer unlock()

	u, ok := s.store.GetUpload(id)
	if !ok {
		writeError(w, http.StatusNotFound, errors.New("upload not found"))
		return
	}
	w.Header().Set("Upload-Length", strconv.FormatInt(u.Length, 10))
	w.Header().Set("Upload-Offset", strconv.FormatInt(u.Offset, 10))
	if r.Method == http.MethodHead {
		if u.Completed {
			w.Header().Set("Upload-Complete", "true")
		}
		w.WriteHeader(http.StatusNoContent)
		return
	}
	if u.Completed {
		writeError(w, http.StatusConflict, errors.New("upload already completed"))
		return
	}
	offsetHeader := r.Header.Get("X-Upload-Offset")
	if offsetHeader == "" {
		offsetHeader = r.Header.Get("Upload-Offset")
	}
	offset, err := strconv.ParseInt(offsetHeader, 10, 64)
	if err != nil || offset != u.Offset {
		w.Header().Set("Upload-Offset", strconv.FormatInt(u.Offset, 10))
		writeError(w, http.StatusConflict, fmt.Errorf("offset mismatch, server=%d", u.Offset))
		return
	}
	// 只能写入声明的剩余字节；下面的 io.CopyN 也能防止客户端数据意外吞掉
	// 下一个请求的内容。
	if r.ContentLength > u.Length-offset {
		writeError(w, http.StatusRequestEntityTooLarge, errors.New("chunk exceeds upload length"))
		return
	}
	f, err := os.OpenFile(u.Path, os.O_CREATE|os.O_WRONLY, 0o600)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	defer f.Close()
	if _, err := f.Seek(offset, io.SeekStart); err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	limit := u.Length - offset
	if r.ContentLength >= 0 && r.ContentLength < limit {
		limit = r.ContentLength
	}
	written, err := io.CopyN(f, r.Body, limit)
	if err != nil && !errors.Is(err, io.EOF) {
		writeError(w, http.StatusBadRequest, fmt.Errorf("write upload chunk: %w", err))
		return
	}
	if err := f.Sync(); err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	newOffset := offset + written
	completed := newOffset == u.Length
	u, err = s.store.UpdateUpload(id, func(upload *store.Upload) error { upload.Offset = newOffset; upload.Completed = completed; return nil })
	if err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	if completed {
		t, transferErr := s.transfers.StartDriveUpload(u)
		if transferErr != nil {
			writeError(w, http.StatusInternalServerError, transferErr)
			return
		}
		w.Header().Set("X-Transfer-ID", t.ID)
	}
	w.Header().Set("Upload-Offset", strconv.FormatInt(newOffset, 10))
	w.WriteHeader(http.StatusNoContent)
}

// handleDeleteUpload 删除尚未进入 Drive worker 的本地暂存上传。
func (s *Server) handleDeleteUpload(w http.ResponseWriter, id string) {
	unlock := s.lockUpload(id)
	defer unlock()

	// 已完成的本地暂存上传已经交给 Drive worker，不能通过此接口删除；
	// 如需终止，应取消对应的传输任务。
	u, ok := s.store.GetUpload(id)
	if !ok {
		writeError(w, http.StatusNotFound, errors.New("upload not found"))
		return
	}
	if u.Completed {
		writeError(w, http.StatusConflict, errors.New("upload is already queued for Drive transfer"))
		return
	}
	if err := os.Remove(u.Path); err != nil && !errors.Is(err, os.ErrNotExist) {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	if err := s.store.DeleteUpload(id); err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

// handleTransferGet 返回单个传输任务的当前状态和进度。
func (s *Server) handleTransferGet(w http.ResponseWriter, id string) {
	t, ok := s.store.GetTransfer(id)
	if !ok {
		writeError(w, http.StatusNotFound, errors.New("transfer not found"))
		return
	}
	writeJSON(w, http.StatusOK, t)
}

// handleTransferAction 执行传输任务的暂停、恢复或取消操作。
func (s *Server) handleTransferAction(w http.ResponseWriter, id, action string) {
	var err error
	switch action {
	case "pause":
		err = s.transfers.Pause(id)
	case "resume":
		err = s.transfers.Resume(id)
	case "cancel":
		err = s.transfers.Cancel(id)
	default:
		writeError(w, http.StatusNotFound, errors.New("unknown transfer action"))
		return
	}
	if err != nil {
		writeError(w, http.StatusConflict, err)
		return
	}
	t, _ := s.store.GetTransfer(id)
	writeJSON(w, http.StatusOK, t)
}

// setCommonHeaders 设置本地前端所需的 CORS、允许方法和可暴露响应头。
func (s *Server) setCommonHeaders(w http.ResponseWriter, r *http.Request) {
	if origin := r.Header.Get("Origin"); origin == "http://127.0.0.1:8000" || origin == "http://localhost:8000" {
		// sidecar 仅供本地使用，只允许 Python UI 使用的两个本地来源，
		// 不向任意来源返回 CORS 许可。
		w.Header().Set("Access-Control-Allow-Origin", origin)
	}
	w.Header().Set("Access-Control-Allow-Methods", "GET,POST,PATCH,HEAD,DELETE,OPTIONS")
	w.Header().Set("Access-Control-Allow-Headers", "Content-Type,Range,Authorization,X-API-Token,X-Upload-Length,X-Upload-Offset,Upload-Length,Upload-Offset,X-File-Name,X-File-Name-Encoded,X-File-Mime,X-Client-Request-ID,Idempotency-Key")
	w.Header().Set("Access-Control-Expose-Headers", "Content-Length,Content-Range,Accept-Ranges,ETag,Location,Upload-Offset,Upload-Length,X-Transfer-ID,X-Client-Request-ID")
	w.Header().Add("Vary", "Origin")
}

// writeDriveError 将 Drive/OAuth 错误映射为适合前端处理的 HTTP 状态码。
func writeDriveError(w http.ResponseWriter, err error) {
	status := http.StatusBadGateway
	message := err.Error()
	if strings.Contains(message, "尚未完成 Google Drive 授权") ||
		strings.Contains(message, "ClientID") ||
		strings.Contains(message, "invalid_grant") ||
		strings.Contains(message, "invalid_token") {
		status = http.StatusServiceUnavailable
	}
	writeError(w, status, err)
}

// writeError 以统一 JSON 格式写入错误响应。
func writeError(w http.ResponseWriter, status int, err error) {
	writeJSON(w, status, map[string]any{"error": err.Error()})
}

// writeJSON 设置 JSON 响应头并编码响应正文。
func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

// copyHeader 将指定的非空响应头从源请求复制到目标响应。
func copyHeader(dst, src http.Header, keys ...string) {
	for _, key := range keys {
		if value := src.Get(key); value != "" {
			dst.Set(key, value)
		}
	}
}

// splitPath 清理 URL 两端斜杠并拆分出非空路径片段。
func splitPath(value string) []string {
	parts := strings.Split(strings.Trim(value, "/"), "/")
	result := make([]string, 0, len(parts))
	for _, part := range parts {
		if part != "" {
			result = append(result, part)
		}
	}
	return result
}
