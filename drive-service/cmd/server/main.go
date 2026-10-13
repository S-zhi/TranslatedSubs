// server 命令启动供 Python Web 应用使用的本地 Google Drive sidecar。
// 进程内的各项服务依赖均以单例方式创建。
package main

import (
	"flag"
	"log"
	"net/http"
	"os"
	"time"

	"github.com/s-zhi/subtitles-ai-drive/internal/config"
	driveclient "github.com/s-zhi/subtitles-ai-drive/internal/drive"
	"github.com/s-zhi/subtitles-ai-drive/internal/httpapi"
	"github.com/s-zhi/subtitles-ai-drive/internal/oauth"
	"github.com/s-zhi/subtitles-ai-drive/internal/store"
	"github.com/s-zhi/subtitles-ai-drive/internal/transfer"
)

// main 加载配置、组装单例服务、恢复任务并启动本地 HTTP 服务。
func main() {
	configPath := flag.String("config", "./config.local.json", "path to local JSON config")
	flag.Parse()

	cfg, err := config.Load(*configPath)
	if err != nil {
		log.Printf("配置文件未加载: %v；使用默认配置", err)
		cfg = config.Default()
	}
	if err := cfg.EnsureDataDir(); err != nil {
		log.Fatal(err)
	}
	if err := os.MkdirAll(cfg.StagingDir(), 0o700); err != nil {
		log.Fatal(err)
	}
	state, err := store.Open(cfg.StatePath())
	if err != nil {
		log.Fatal(err)
	}
	auth := oauth.NewManager(&cfg)
	drive := driveclient.NewClient(&cfg, auth, state)
	transfers := transfer.NewManager(&cfg, state, drive)
	// 在接收新请求前，重新调度持久化记录中尚未结束的传输任务。
	transfers.Recover()

	// ReadHeaderTimeout 只限制请求头解析时间。请求体的持续传输由请求上下文
	// 和传输流水线控制；IdleTimeout 作用于 keep-alive 请求之间的空闲时间，
	// 不会限制上传数据本身的持续时间。
	server := &http.Server{
		Addr:              cfg.ListenAddr,
		Handler:           httpapi.New(&cfg, auth, drive, transfers, state),
		ReadHeaderTimeout: 15 * time.Second,
		IdleTimeout:       120 * time.Second,
	}
	log.Printf("TranslatedSubs Drive service listening on http://%s", cfg.ListenAddr)
	log.Printf("Google OAuth credentials are intentionally empty until configured")
	if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatal(err)
	}
}
