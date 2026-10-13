package httpapi

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"github.com/s-zhi/subtitles-ai-drive/internal/config"
	driveclient "github.com/s-zhi/subtitles-ai-drive/internal/drive"
	"github.com/s-zhi/subtitles-ai-drive/internal/oauth"
	"github.com/s-zhi/subtitles-ai-drive/internal/store"
	"github.com/s-zhi/subtitles-ai-drive/internal/transfer"
)

type taskFolderDriveStub struct {
	files   []driveclient.File
	creates int
}

func (d *taskFolderDriveStub) List(_ context.Context, _, _ string, _ int64) (*driveclient.FileList, error) {
	return &driveclient.FileList{Files: append([]driveclient.File(nil), d.files...)}, nil
}

func (d *taskFolderDriveStub) CreateFolder(_ context.Context, name, _ string, properties map[string]string) (*driveclient.File, error) {
	d.creates++
	copyProperties := make(map[string]string, len(properties))
	for key, value := range properties {
		copyProperties[key] = value
	}
	folder := driveclient.File{
		ID:            "task-folder-1",
		Name:          name,
		MimeType:      "application/vnd.google-apps.folder",
		AppProperties: copyProperties,
	}
	d.files = append(d.files, folder)
	return &folder, nil
}

func (d *taskFolderDriveStub) ValidateFolderUnderRoot(context.Context, string) (*driveclient.File, error) {
	return nil, nil
}

func (d *taskFolderDriveStub) Metadata(context.Context, string) (*driveclient.File, error) {
	return nil, nil
}

func (d *taskFolderDriveStub) Trash(context.Context, string) error { return nil }

func (d *taskFolderDriveStub) DownloadRange(context.Context, string, string) (*driveclient.File, *http.Response, error) {
	return nil, nil, nil
}

// TestUploadEndpointSupportsOffsetChecks 验证分片上传的偏移校验、查询和删除流程。
func TestUploadEndpointSupportsOffsetChecks(t *testing.T) {
	t.Parallel()
	cfg := config.Default()
	cfg.DataDir = t.TempDir()
	cfg.PythonBaseURL = ""
	if err := cfg.EnsureDataDir(); err != nil {
		t.Fatal(err)
	}
	state, err := store.Open(filepath.Join(cfg.DataDir, "state.json"))
	if err != nil {
		t.Fatal(err)
	}
	auth := oauth.NewManager(&cfg)
	drive := driveclient.NewClient(&cfg, auth, state)
	transfers := transfer.NewManager(&cfg, state, drive)
	server := New(&cfg, auth, drive, transfers, state)

	createReq := httptest.NewRequest(http.MethodPost, "/api/drive/uploads", nil)
	createReq.Header.Set("X-Upload-Length", "10")
	createReq.Header.Set("X-File-Name", "video.mp4")
	createReq.Header.Set("X-File-Mime", "video/mp4")
	createResp := httptest.NewRecorder()
	server.ServeHTTP(createResp, createReq)
	if createResp.Code != http.StatusCreated {
		t.Fatalf("create status = %d body=%s", createResp.Code, createResp.Body.String())
	}
	var created struct {
		ID string `json:"id"`
	}
	if err := json.Unmarshal(createResp.Body.Bytes(), &created); err != nil {
		t.Fatal(err)
	}
	if created.ID == "" {
		t.Fatal("create response has no upload id")
	}

	patchReq := httptest.NewRequest(http.MethodPatch, "/api/drive/uploads/"+created.ID, bytes.NewBufferString("abc"))
	patchReq.Header.Set("X-Upload-Offset", "0")
	patchResp := httptest.NewRecorder()
	server.ServeHTTP(patchResp, patchReq)
	if patchResp.Code != http.StatusNoContent || patchResp.Header().Get("Upload-Offset") != "3" {
		t.Fatalf("patch status=%d offset=%q body=%s", patchResp.Code, patchResp.Header().Get("Upload-Offset"), patchResp.Body.String())
	}

	headReq := httptest.NewRequest(http.MethodHead, "/api/drive/uploads/"+created.ID, nil)
	headResp := httptest.NewRecorder()
	server.ServeHTTP(headResp, headReq)
	if headResp.Code != http.StatusNoContent || headResp.Header().Get("Upload-Offset") != "3" {
		t.Fatalf("head status=%d offset=%q", headResp.Code, headResp.Header().Get("Upload-Offset"))
	}

	mismatchReq := httptest.NewRequest(http.MethodPatch, "/api/drive/uploads/"+created.ID, bytes.NewBufferString("x"))
	mismatchReq.Header.Set("X-Upload-Offset", "0")
	mismatchResp := httptest.NewRecorder()
	server.ServeHTTP(mismatchResp, mismatchReq)
	if mismatchResp.Code != http.StatusConflict || mismatchResp.Header().Get("Upload-Offset") != "3" {
		t.Fatalf("mismatch status=%d offset=%q body=%s", mismatchResp.Code, mismatchResp.Header().Get("Upload-Offset"), mismatchResp.Body.String())
	}

	deleteReq := httptest.NewRequest(http.MethodDelete, "/api/drive/uploads/"+created.ID, nil)
	deleteResp := httptest.NewRecorder()
	server.ServeHTTP(deleteResp, deleteReq)
	if deleteResp.Code != http.StatusNoContent {
		t.Fatalf("delete status=%d body=%s", deleteResp.Code, deleteResp.Body.String())
	}
	missingHead := httptest.NewRecorder()
	server.ServeHTTP(missingHead, headReq)
	if missingHead.Code != http.StatusNotFound {
		t.Fatalf("deleted upload HEAD status=%d", missingHead.Code)
	}
}

// TestConcurrentUploadChunksSerialize verifies duplicate offsets cannot overwrite one another.
func TestConcurrentUploadChunksSerialize(t *testing.T) {
	t.Parallel()
	cfg := config.Default()
	cfg.DataDir = t.TempDir()
	if err := cfg.EnsureDataDir(); err != nil {
		t.Fatal(err)
	}
	state, err := store.Open(filepath.Join(cfg.DataDir, "state.json"))
	if err != nil {
		t.Fatal(err)
	}
	auth := oauth.NewManager(&cfg)
	drive := driveclient.NewClient(&cfg, auth, state)
	transfers := transfer.NewManager(&cfg, state, drive)
	server := New(&cfg, auth, drive, transfers, state)

	createReq := httptest.NewRequest(http.MethodPost, "/api/drive/uploads", nil)
	createReq.Header.Set("X-Upload-Length", "4")
	createResp := httptest.NewRecorder()
	server.ServeHTTP(createResp, createReq)
	if createResp.Code != http.StatusCreated {
		t.Fatalf("create status = %d body=%s", createResp.Code, createResp.Body.String())
	}
	var created struct {
		ID string `json:"id"`
	}
	if err := json.Unmarshal(createResp.Body.Bytes(), &created); err != nil {
		t.Fatal(err)
	}

	responses := make([]*httptest.ResponseRecorder, 2)
	bodies := []string{"abc", "xyz"}
	var wg sync.WaitGroup
	for i := range bodies {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			req := httptest.NewRequest(http.MethodPatch, "/api/drive/uploads/"+created.ID, bytes.NewBufferString(bodies[i]))
			req.Header.Set("X-Upload-Offset", "0")
			responses[i] = httptest.NewRecorder()
			server.ServeHTTP(responses[i], req)
		}(i)
	}
	wg.Wait()

	successes, conflicts := 0, 0
	for _, response := range responses {
		switch response.Code {
		case http.StatusNoContent:
			successes++
			if response.Header().Get("Upload-Offset") != "3" {
				t.Fatalf("success offset = %q", response.Header().Get("Upload-Offset"))
			}
		case http.StatusConflict:
			conflicts++
			if response.Header().Get("Upload-Offset") != "3" {
				t.Fatalf("conflict offset = %q", response.Header().Get("Upload-Offset"))
			}
		default:
			t.Fatalf("unexpected concurrent PATCH status = %d body=%s", response.Code, response.Body.String())
		}
	}
	if successes != 1 || conflicts != 1 {
		t.Fatalf("successes=%d conflicts=%d, want one each", successes, conflicts)
	}
	upload, ok := state.GetUpload(created.ID)
	if !ok {
		t.Fatal("upload disappeared")
	}
	content, err := os.ReadFile(upload.Path)
	if err != nil {
		t.Fatal(err)
	}
	if string(content) != "abc" && string(content) != "xyz" {
		t.Fatalf("file content = %q, want one complete chunk", content)
	}
	if upload.Offset != int64(len(content)) || upload.Offset != 3 {
		t.Fatalf("offset=%d content length=%d", upload.Offset, len(content))
	}
}

// TestLocalhostCORSOriginIsReflected 验证允许的本地前端来源会收到 CORS 响应头。
func TestLocalhostCORSOriginIsReflected(t *testing.T) {
	t.Parallel()
	cfg := config.Default()
	cfg.DataDir = t.TempDir()
	state, err := store.Open(filepath.Join(cfg.DataDir, "state.json"))
	if err != nil {
		t.Fatal(err)
	}
	auth := oauth.NewManager(&cfg)
	drive := driveclient.NewClient(&cfg, auth, state)
	transfers := transfer.NewManager(&cfg, state, drive)
	server := New(&cfg, auth, drive, transfers, state)
	req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	req.Header.Set("Origin", "http://localhost:8000")
	resp := httptest.NewRecorder()
	server.ServeHTTP(resp, req)
	if got := resp.Header().Get("Access-Control-Allow-Origin"); got != "http://localhost:8000" {
		t.Fatalf("allow origin = %q", got)
	}
	if got := resp.Header().Get("Access-Control-Allow-Headers"); !strings.Contains(got, "X-File-Name-Encoded") {
		t.Fatalf("allow headers = %q", got)
	}
}

func TestUploadFileNameHeaderDecodesUnicodeAndRejectsControls(t *testing.T) {
	t.Parallel()
	const original = "AI学习 + 100%.mp4"
	req := httptest.NewRequest(http.MethodPost, "/api/drive/uploads", nil)
	req.Header.Set("X-File-Name-Encoded", url.PathEscape(original))
	name, provided, err := uploadFileNameHeader(req)
	if err != nil || !provided || name != original {
		t.Fatalf("decoded name=%q provided=%v err=%v", name, provided, err)
	}

	bad := httptest.NewRequest(http.MethodPost, "/api/drive/uploads", nil)
	bad.Header.Set("X-File-Name-Encoded", "bad%0Aname.mp4")
	if _, _, err := uploadFileNameHeader(bad); err == nil {
		t.Fatal("encoded control character should be rejected")
	}
}

func TestFolderUploadManifestIsSafeAndIdempotent(t *testing.T) {
	t.Parallel()
	cfg := config.Default()
	cfg.DataDir = t.TempDir()
	state, err := store.Open(filepath.Join(cfg.DataDir, "state.json"))
	if err != nil {
		t.Fatal(err)
	}
	auth := oauth.NewManager(&cfg)
	drive := driveclient.NewClient(&cfg, auth, state)
	transfers := transfer.NewManager(&cfg, state, drive)
	server := New(&cfg, auth, drive, transfers, state)

	unsafeReq := httptest.NewRequest(http.MethodPost, "/api/drive/folder-uploads", bytes.NewBufferString(`{"entries":[{"relativePath":"../escape.txt","size":1}]}`))
	unsafeReq.Header.Set("Content-Type", "application/json")
	unsafeResp := httptest.NewRecorder()
	server.ServeHTTP(unsafeResp, unsafeReq)
	if unsafeResp.Code != http.StatusBadRequest {
		t.Fatalf("unsafe manifest status = %d body=%s", unsafeResp.Code, unsafeResp.Body.String())
	}

	body := `{"entries":[{"relativePath":"video.mp4","size":1,"mime":"video/mp4"}]}`
	createReq := httptest.NewRequest(http.MethodPost, "/api/drive/folder-uploads", bytes.NewBufferString(body))
	createReq.Header.Set("Content-Type", "application/json")
	createReq.Header.Set("Idempotency-Key", "folder-request-1")
	createResp := httptest.NewRecorder()
	server.ServeHTTP(createResp, createReq)
	if createResp.Code != http.StatusCreated {
		t.Fatalf("create folder upload status = %d body=%s", createResp.Code, createResp.Body.String())
	}
	var first folderUploadResponse
	if err := json.Unmarshal(createResp.Body.Bytes(), &first); err != nil {
		t.Fatal(err)
	}
	if first.Batch.ID == "" || len(first.Entries) != 1 || first.Entries[0].RelativePath != "video.mp4" {
		t.Fatalf("folder response = %#v", first)
	}

	retryReq := httptest.NewRequest(http.MethodPost, "/api/drive/folder-uploads", bytes.NewBufferString(body))
	retryReq.Header.Set("Content-Type", "application/json")
	retryReq.Header.Set("Idempotency-Key", "folder-request-1")
	retryResp := httptest.NewRecorder()
	server.ServeHTTP(retryResp, retryReq)
	if retryResp.Code != http.StatusOK {
		t.Fatalf("idempotent retry status = %d body=%s", retryResp.Code, retryResp.Body.String())
	}
	var second folderUploadResponse
	if err := json.Unmarshal(retryResp.Body.Bytes(), &second); err != nil {
		t.Fatal(err)
	}
	if second.Batch.ID != first.Batch.ID || len(second.Entries) != 1 || second.Entries[0].ID != first.Entries[0].ID {
		t.Fatalf("idempotent retry = %#v, first=%#v", second, first)
	}
}

func TestTaskFolderEndpointUsesUniqueTaskIDAndIsIdempotent(t *testing.T) {
	t.Parallel()
	cfg := config.Default()
	auth := oauth.NewManager(&cfg)
	drive := &taskFolderDriveStub{}
	server := New(&cfg, auth, drive, nil, nil)
	const taskID = "task_c4b659ea"

	firstReq := httptest.NewRequest(http.MethodPost, "/api/drive/task-folders", strings.NewReader(`{"taskId":"`+taskID+`"}`))
	firstReq.Header.Set("Content-Type", "application/json")
	firstResp := httptest.NewRecorder()
	server.ServeHTTP(firstResp, firstReq)
	if firstResp.Code != http.StatusCreated {
		t.Fatalf("first task folder status = %d body=%s", firstResp.Code, firstResp.Body.String())
	}
	var first struct {
		Folder  driveclient.File `json:"folder"`
		TaskID  string           `json:"taskId"`
		Created bool             `json:"created"`
	}
	if err := json.Unmarshal(firstResp.Body.Bytes(), &first); err != nil {
		t.Fatal(err)
	}
	if first.TaskID != taskID || !first.Created || first.Folder.Name != taskID || first.Folder.AppProperties["subtitles_ai_task_id"] != taskID {
		t.Fatalf("first task folder payload = %#v", first)
	}

	secondReq := httptest.NewRequest(http.MethodPost, "/api/drive/task-folders", strings.NewReader(`{"taskId":"`+taskID+`"}`))
	secondReq.Header.Set("Content-Type", "application/json")
	secondResp := httptest.NewRecorder()
	server.ServeHTTP(secondResp, secondReq)
	if secondResp.Code != http.StatusOK {
		t.Fatalf("idempotent task folder status = %d body=%s", secondResp.Code, secondResp.Body.String())
	}
	var second struct {
		Folder  driveclient.File `json:"folder"`
		Created bool             `json:"created"`
	}
	if err := json.Unmarshal(secondResp.Body.Bytes(), &second); err != nil {
		t.Fatal(err)
	}
	if second.Created || second.Folder.ID != first.Folder.ID || drive.creates != 1 {
		t.Fatalf("idempotent task folder payload = %#v, creates=%d", second, drive.creates)
	}

	unsafeReq := httptest.NewRequest(http.MethodPost, "/api/drive/task-folders", strings.NewReader(`{"taskId":"../title"}`))
	unsafeResp := httptest.NewRecorder()
	server.ServeHTTP(unsafeResp, unsafeReq)
	if unsafeResp.Code != http.StatusBadRequest {
		t.Fatalf("unsafe task id status = %d body=%s", unsafeResp.Code, unsafeResp.Body.String())
	}
}

type cleanupDriveStub struct {
	taskFolderDriveStub
	failOnPath  string
	createdDirs []string
	trashed     []string
}

func (d *cleanupDriveStub) CreateFolder(_ context.Context, name, parentID string, properties map[string]string) (*driveclient.File, error) {
	relPath := properties["subtitles_ai_relative_path"]
	if relPath == d.failOnPath {
		return nil, errorDriveStub{msg: "failed to create folder " + relPath}
	}
	id := "folder-id-" + name
	d.createdDirs = append(d.createdDirs, id)
	return &driveclient.File{
		ID:            id,
		Name:          name,
		MimeType:      "application/vnd.google-apps.folder",
		AppProperties: properties,
	}, nil
}

func (d *cleanupDriveStub) Trash(_ context.Context, fileID string) error {
	d.trashed = append(d.trashed, fileID)
	return nil
}

type errorDriveStub struct {
	msg string
}

func (e errorDriveStub) Error() string { return e.msg }

func (d *cleanupDriveStub) createErr(path string) error {
	return errorDriveStub{msg: "failed to create folder " + path}
}

func TestFolderUploadCleansUpCreatedFoldersOnFailure(t *testing.T) {
	t.Parallel()
	cfg := config.Default()
	cfg.DataDir = t.TempDir()
	state, err := store.Open(filepath.Join(cfg.DataDir, "state.json"))
	if err != nil {
		t.Fatal(err)
	}
	auth := oauth.NewManager(&cfg)

	stub := &cleanupDriveStub{failOnPath: "season-1/episode-2"}
	server := New(&cfg, auth, stub, nil, state)

	body := `{"entries":[
		{"relativePath":"season-1/episode-1/v1.mp4","size":10},
		{"relativePath":"season-1/episode-2/v2.mp4","size":10}
	]}`

	req := httptest.NewRequest(http.MethodPost, "/api/drive/folder-uploads", bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	resp := httptest.NewRecorder()
	server.ServeHTTP(resp, req)

	if resp.Code == http.StatusCreated {
		t.Fatalf("expected folder creation failure, got %d", resp.Code)
	}

	// Paths sorted by depth/alphabetical: season-1 (depth 0), season-1/episode-1 (depth 1), season-1/episode-2 (depth 1 fails)
	// Created dirs should be season-1 and episode-1.
	if len(stub.createdDirs) != 2 {
		t.Fatalf("expected 2 created dirs before failure, got %v", stub.createdDirs)
	}

	// Trashed should be in reverse order (episode-1 then season-1)
	if len(stub.trashed) != 2 {
		t.Fatalf("expected 2 trashed dirs on cleanup, got %v", stub.trashed)
	}
	if stub.trashed[0] != "folder-id-episode-1" || stub.trashed[1] != "folder-id-season-1" {
		t.Fatalf("unexpected trash order: %v", stub.trashed)
	}

	batches := state.ListFolderBatches()
	if len(batches) != 1 || batches[0].State != transfer.StateFailed {
		t.Fatalf("expected 1 failed batch, got %#v", batches)
	}
}
