# 纸书智伴

当前主线已接通 PDF 导入、逐页阅读、朗读与动画任务、配音整片导出，以及带原页引用的问答和会话恢复。P0/P1 的实现已进入 `app/`，并有本地测试及受控服务验证；**真实模型 API、识别/回答质量和首音时延尚未验收**。当前按“先本地与受控验证，再推送”的范围推进。

## 目录与入口

- [`app/`](app/) 提供整本 PDF 工作台（`/`、`/books`）、单页速读（`/quick`、`/quick.html`）和主题故事创作（`/tts_v2.html`）。
- [`reference/pdf-import/`](reference/pdf-import/)：冻结的原 PDF 分支参考，保持原样，不用于覆盖主线。
- [`tests/`](tests/)：本地回归与受控应用；测试样例在临时目录生成。
- [`docs/`](docs/)：功能状态、路线、验证与 API 约定。

[源码来源与原始导入清单](docs/SOURCE_PROVENANCE.md)记录历史快照，不代表当前主线的逐文件状态。

## 当前功能

| 功能 | 当前边界 |
| --- | --- |
| PDF 自动识字/完整看图/文字层模式 | 保留所有原页和稳定页号；缺配置、空白响应及失败明确标记 |
| 单页速读 | 上传 PDF、预览选页、生成旁白和动画、点读、按页问答。媒体服务不可用时保留原图和文字旁白 |
| 拍照页图入库 | 接收单页或有序多页照片，保存原页图、OCR、画面语义和归一化实体框 |
| 原图、逐页朗读与动画 | 不等待整本视频完成即可阅读；模型适配器实际效果未验收 |
| 整片、字幕与时间轴 | 本地 FFmpeg 合成 H264/AAC，默认 1280×720/24fps；音频与画面时钟明确区分 |
| 点读、原页引用与会话 | 显式像素坐标；关键词/bigram 检索、引用校验及媒体引用持久化 |
| 真实服务质量与低延迟 | 待验收；受控输出不能作为模型成绩 |

详细状态见[功能状态](docs/FEATURE_STATUS.md)，接口见[书本 API](docs/BOOK_API.md)。

## 本地启动

在仓库根目录执行以下 Windows PowerShell 命令。当前本地验证使用 Python 3.12；依赖安装和工具准备仍应在目标机器检查。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
cd app
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
..\.venv\Scripts\python.exe story_tts_server.py
```

打开 `http://127.0.0.1:5000/` 或 `http://127.0.0.1:5000/books`。`HOST` 默认 `127.0.0.1`，`PORT` 默认 `5000`，均可通过环境变量配置。

在 Windows 安装依赖后，也可双击 [`app/启动服务.bat`](app/启动服务.bat)。服务开始监听后会打开浏览器的单页速读入口。直接打开 `quick.html` 时，页面会显示本地启动方法。

在工作台点「单页速读」可只处理所选 PDF 页。扫描页识字和图片入库需要配置视觉服务，也可安装可选的 `rapidocr-onnxruntime` 做本地 OCR，画面结构化仍使用配置的视觉服务。

原图点读会标出模型识别到的物体，点击「物体框」可切换显示。动画播放时，点击画面或「原图点读」可打开带框原页。

[配置模板](app/.env.example)不含可用凭据，服务密钥与模型配置需自行填写；留空能力会明确显示未配置。PDF 原图与文字层读取可以先用，真实 OCR/视觉、视频和模型问答需要对应配置。[能力接口](docs/BOOK_API.md)只检查配置和本机依赖，不做在线认证。

- FFmpeg 和 ffprobe 需加入 `PATH`，或设置 `FFMPEG_PATH`、`FFPROBE_PATH`。
- ASR 权重不随仓库分发。`WHISPER_MODEL` 指向本地模型，默认 `WHISPER_ALLOW_DOWNLOAD=false`，不会自动下载。
- `edge-tts` 仍需网络；TTS 显示本机已准备不代表网络合成已验证。
- 文生图、音乐、音效为可选配置；新 PDF 管线直接保留原页图，不要求先重绘。
- 运行数据默认写入 `app/output_dir/books/`；可通过 `TARGET_DIR` 更改数据根目录。不要提交 `.env`、权重或运行产物。

### 安全与部署

- 浏览器默认可从同源和服务端口上的 `http://127.0.0.1`、`http://localhost`、`http://[::1]` 访问。`CORS_ALLOW_ORIGINS` 可在 `app/.env` 中追加逗号分隔的来源，例如 `https://reader.example.com,http://localhost:5173`。来源只由协议、主机和端口组成，`/tts-story-book` 前缀无需单独配置。留空 `API_TOKEN` 时，API 只接受回环主机或此列表中显式配置的访问地址。
- 写入请求优先检查 `Origin`，缺少时检查有值的 `Referer`。两者都缺少时，通过来源检查，便于使用 curl 等客户端。
- `HOST` 设置为非回环地址（如 `0.0.0.0`）时，必须设置 `API_TOKEN` 才能启动。设置后，`/api/*` 及其带前缀的地址要求 `Authorization: Bearer <token>` 或 `X-API-Token: <token>`。允许来源的 CORS 预检、`/health` 和静态页面可直接访问。
- 现有页面的 API 请求不携带令牌，直接使用工作台时请保持默认回环监听并留空 `API_TOKEN`。跨机器调用使用能发送令牌的客户端，并通过 HTTPS 传输。
- 同一主机上共用 `TARGET_DIR` 的服务进程通过 `.bookshelf.lock` 同步书架操作。请使用支持文件锁的本地文件系统，并在服务运行期间保留锁文件。获取锁失败或等候超时会返回 HTTP 503。
- 精确移除书架记录时，向 `POST /api/bookshelf/remove` 发送列表中的 `session_dir`、`name` 和 `id` 或 `created_at`。无 ID 的旧记录可用路径和名称匹配首条记录。旧客户端只传路径时，该路径必须唯一。仍被其他记录引用的目录会保留，响应包含 `warning`。

## 受控复现

从仓库根目录运行：

```powershell
.\.venv\Scripts\python.exe -m pytest
node tests/js/test_book_ui.cjs
.\.venv\Scripts\python.exe tests/controlled_app.py --data-dir .local/controlled --port 18782
```

受控应用打开 `http://127.0.0.1:18782/`，生成的三页混合 PDF 位于 `.local/controlled/controlled-mixed.pdf`。它使用真实 Flask、PDF 和 FFmpeg，但 OCR、回答、配音及动画输出经过控制，**不能作为真实模型质量或真实接口时延结果**。

## 协作入口

- [贡献约定](CONTRIBUTING.md)
- [功能状态与已知边界](docs/FEATURE_STATUS.md)
- [开发路线与验收条件](docs/ROADMAP.md)
- [验证记录与复现命令](docs/VALIDATION.md)
- [书本 API 与时间轴](docs/BOOK_API.md)
- [源码来源与历史导入范围](docs/SOURCE_PROVENANCE.md)
