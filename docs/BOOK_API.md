# 书本 API

当前主线的本地接口。实现见[书本管线](../app/book_pipeline.py)、[PDFReader](../app/pdf_book_reader.py)、[整片模块](../app/book_video.py)与[服务适配](../app/story_tts_server.py)。默认使用 `/api`；已有中间件兼容 `/tts-story-book/api`。客户端应使用返回的资源 URL，不拼接本机文件系统路径。

## 返回约定

- 普通成功返回 `{ "success": true, ... }`；可预期错误返回 `{ "success": false, "error": "说明" }` 及对应 HTTP 状态。
- 创建导入、重分析、媒体、导出或重试任务时，HTTP 202 返回 `{ "success": true, "book_id": "书本ID", "job_id": "任务ID" }`。这仅表示已接受，不能当作生成完成。
- 查询任务的 `success:true` 表示查询成功；仍需读取 `job.status`。同理，导入任务完成但 `book.status=needs_ocr/degraded` 时，不能声称完整理解成功。
- 标识与资源引用不得包含绝对路径、`..` 或跨书引用。错误说明不应泄漏凭据或本机位置。

## 主要路由

| 方法与路径 | 请求 | 成功结果中的主要字段 |
| --- | --- | --- |
| GET `/api/capabilities` | 无 | `capabilities` 扁平布尔值、`online_verified:false`；仅配置/本机检查 |
| POST `/api/books/import` | multipart：`file` PDF、`mode`、`verify_ocr`，可选 `title` | 202：`book_id, job_id` |
| GET `/api/books` | 无 | `books` 摘要数组 |
| GET `/api/books/<book_id>` | 无 | `book` manifest |
| GET `/api/books/<book_id>/assets/<relative>` | 本书受限相对资源 | 图片、音频、视频或导出文件 |
| POST `/api/books/<book_id>/analyze` | JSON：`mode, verify_ocr` | 202：重分析任务 |
| POST `/api/books/<book_id>/media` | JSON：`voice`，可选 `page_ids, force, video_options` | 202：媒体任务 |
| GET `/api/jobs/<job_id>` | 无 | `job` 状态、进度、错误和结果 |
| POST `/api/jobs/<job_id>/retry` | JSON 对象 | 202：同一任务显式重试 |
| POST `/api/books/<book_id>/export` | JSON：可选 `force, video_options` | 202：整片任务 |
| POST `/api/books/<book_id>/recognize` | JSON：`page_id, source(image/video/film), frame_time, request_id` | 显式坐标 `objects`、`cached`、`timing_ms` 与上下文回传 |
| POST `/api/books/<book_id>/speak` | JSON：`text, voice`，可选 `language` | `audio_url, audio_ref, duration` |
| POST `/api/books/<book_id>/qa` | JSON：问题、会话与可选页/时间/媒体引用 | `session_id, answer, answer_source, citations, moments, turn, timing_ms` |
| GET `/api/books/<book_id>/search` | 查询：`q`（1–200 字），可选 `page_id, limit`(1–10) | `moments, film, timing_ms`；不调用模型 |
| GET `/api/books/<book_id>/sessions/<session_id>` | 无 | `session` 及其 `turns` |
| POST `/api/books/<book_id>/attachments` | multipart：`file, kind=audio/image` | `ref, url` |
| POST `/api/books/<book_id>/transcribe` | multipart：`file`（兼容 `audio`），可选 `language` | `text, audio_ref, audio_url` |

常见错误包括：400 参数或路径无效、404 不存在、409 不可重试状态、413 上传超限、422 未识别到语音、502 适配器失败/模型结果不合契约、503 能力未配置。异步任务的生成失败主要通过 `job.status=failed` 和 `job.error` 返回。

## 单页速读与页图入库

| 方法与路径 | 请求 | 成功结果中的主要字段 |
| --- | --- | --- |
| POST `/api/quickbook/upload` | multipart `file` 或 `pdf` | `upload_id, page_count, page, image_url` |
| POST `/api/quickbook/preview` | JSON `upload_id, page` | `page, page_count, image_url` |
| POST `/api/quickbook/start` | JSON `upload_id, page, voice`（可选） | `job_id, book_id, page, page_count` |
| GET `/api/quickbook/status/<job_id>` | 无 | `status, stage, progress, warnings` |
| GET `/api/quickbook/result/<job_id>` | 无 | `image_url, video_url, narration, entities, warnings`。未就绪或失败返回 409 |
| POST `/api/quickbook/point` | JSON `job_id, x, y`（`0–1`），可选 `allow_vision` | `matched, source`，命中时附 `bbox, note, audio_url` |
| POST `/api/quickbook/ask` | JSON `job_id, question`（最多 200 字） | `answer, source, audio_url`，未生成音频时为 `null` |
| POST `/api/ingest/page` | multipart `file` 或 JSON `image_base64`，可选 `book_id, title, page, deskew` | `book_id, manifest_path, page_count, page` |
| POST `/api/ingest/book` | multipart 多个 `images`，可用 `orders` 指定页码。也接收 JSON `images` 数组 | `book_id, manifest_path, page_count, pages, summary` |
| GET `/api/book/<book_id>` | 无 | `book` 和相对于数据根的 `manifest_path` |

单页速读优先使用 PDF 文字层，扫描页走 OCR。缺少配音或动画时，播放器显示文字旁白和原页图，并报告原因。文字区和实体框在原页坐标中点读。问答支持 `model` 和 `page_content` 两种来源。返回的 `/api/quickbook/assets/…` 和 `/api/ingest/assets/…` URL 提供生成的媒体。同样可搭配 `/tts-story-book` 前缀访问。

整本和单页上传通过 PDF 解析器验证内容，可处理文件名无扩展名或含前导字节的可读 PDF。空文件、非 PDF、损坏、密码保护及页数越界会在进入生成流程前报告。

单页的 `entities[].bbox` 按原页图的 `normalized_0_1` 左上、右下坐标标注。点位落入嵌套实体时优先命中较小的框。视觉补识别用原页缩略图理解场景，用裁图定位物体。服务端检查返回框包含点击点，再将它转换到原页坐标。

页图入库的坐标空间为 `normalized_0_1`。JSON 的 `images` 可以是 base64 字符串或含 `image_base64`、`filename`、`order` 的对象。页码从 1 开始。单张图片最多 16 MiB，整个请求及总页数受 `BOOK_MAX_UPLOAD_BYTES` 和 `BOOK_MAX_PAGES` 限制。数据存放于配置的 `TARGET_DIR` 下，速读上传暂存于 `output_dir/quick_uploads/`，速读结果与入库清单保存于 `bookshelf/`。

视觉、旁白写作、问答和图生视频使用配置的提供方。配置对应能力后，服务会将相关页图、裁图、文本或问题发送给提供方。当前配音通过 Edge TTS 将朗读文本发送给语音服务。

主题故事页的 `POST /api/analyze-video-frame` 接收 `video_path, time_seconds`，可附 `frame_base64`（JPEG/PNG）。`frame_source` 表明使用浏览器帧还是服务端截帧，`frame_size` 是返回 `objects` 像素框的参考尺寸。

## PDF 模式、manifest 与稳定页

`mode` 为 `fast`、`auto` 或 `full`：

| 模式 | 行为 |
| --- | --- |
| fast | 仅取 PDF 文字层，无文字页标为需要 OCR；不调用视觉模型 |
| auto | 文字层非空时保留；缺字页执行 OCR |
| full | 每页执行 OCR、场景分析和物体识别，包括无字页 |

`verify_ocr` 用于识字后的复核，不会把 fast 自动升级为 full。只有明确返回 `[无文字页面]` 才确认无字；空白 OCR 响应为失败。配置不足或部分失败保留原页和错误，不删除页面。

manifest `schema_version=1` 包含：

- `book_id, title, status, created_at, updated_at`；`source.kind=pdf` 与 `source.pdf_sha256`。
- `pages` 始终按原 PDF 排列：`page_id=p0001` 对应 `page_num=1`，空页和封面不改变后页标识。
- 每页具有 `text, text_status, image_path, image_url, image_size`、`scene_analysis`、`objects`、`media`，以及复核/错误状态。`image_path` 相对书目录，URL 可直接访问。
- `text_status` 区分 `pdf_text`、`pdf_text_unverified`、`ocr`、`empty`、`needs_ocr`、`ocr_failed`。场景模型结果标明来源和 `inferred:true`，不能当成原文事实。
- `media.tts` 包含状态、缓存键、相对 `path`、`url`、真实 `duration`、`voice` 与 `sentences`。每句记录 `text,start,end,duration,audio_path,audio_url`，时间相对本页真实配音。
- `media.video` 包含状态、缓存键、相对 `path`、`url`、原短片 `duration` 与参数。未完成媒体不得据路径存在假定成功。
- `exports` 为空或包含最新 `video_url, srt_url, timeline_url`、相对路径、缓存键、时长及 `probe`。重新分析或实际重生成媒体时使旧整片失效。

无字页可用显式静音停留，`tts.status=silent` 并记录原因；未读出的文字不能用静音代替。

## 任务与缓存

任务具有 `job_id, book_id, kind, status, attempts, progress, error, created_at, updated_at, started_at, finished_at, request, result`。

状态流程为 `queued → running → succeeded/failed`。服务重启将未完成任务标为 `interrupted`；只有失败或中断任务可通过 retry 显式继续。任务和 manifest 原子落盘，避免把半成品当完成结果；重启不暗中重放外部模型调用。

媒体缓存身份覆盖原图/文本、模型身份、提示版本、声音及参数；`force:true` 会重新生成。导出缓存另核对输入和产物信息。客户端应逐次刷新 manifest，已完成书页可以先读，不能等待整本视频才显示第一页。

## 坐标与当前帧点读

点读请求示例：

```json
{"page_id":"p0001","source":"video","frame_time":1.25,"request_id":"click-001"}
```

`source` 为 `image`、`video` 或 `film`。`video` 按该页原短片的 `frame_time` 截帧；`film` 的 `frame_time` 是整片时钟，服务端按时间轴 `video_start <= t < video_end` 定位页（返回的 `page_id` 以此为准），从 `book.mp4` 截帧。视频帧按「媒体路径 + 帧序号 + 视觉模型」缓存截图与识别结果（`attachments/frames/`），同一暂停画面重复点击不再调用视觉模型并返回 `cached=true`；原图没有页级检测结果时同样按图像哈希缓存。返回 `book_id,page_id,request_id,source,frame_time,image_ref,image_url`，以及：

```json
{"coordinate_space":"pixel","reference_width":864,"reference_height":480,"source_id":"frame-source","detections":[{"label":"小兔子","box":[100,100,600,400]}]}
```

`box=[x1,y1,x2,y2]` 使用声明参考图像的绝对像素；不能依据数值大小猜归一化。适配器可输入显式 `pixel/normalized_1/normalized_1000`，统一契约输出像素。前端按 `object-fit:contain` 实际内容矩形换算，留黑点击忽略。

点击前记录书、页、媒体路径、原播放状态、时间与请求版本并暂停。切书/页、seek、重新播放或取消后，迟到响应不可画框或恢复旧页。标签朗读使用 speak；仅在上下文不变、原先播放且朗读未取消/拒绝时恢复。朗读文本限1至2000字；播放拒绝不应伪称已播放。

## 整片、句子与时间轴 v2

导出将各页真实 PCM WAV 拼为一条音频，再一次编码 AAC；页短片按配音长度循环/裁剪，统一 H264、默认1280×720/24fps。SRT 是独立字幕文件，不应假定已经烧录进画面。

时间轴 `schema_version=2` 明确区分：

| 字段 | 含义 |
| --- | --- |
| 顶层 `duration` | 全本真实配音累计时长 |
| `video_frames, video_duration` | 按目标帧率量化的总帧数与画面时长 |
| 页 `book_start, book_end` | 该页在整本配音中的时间范围 |
| 页 `video_start_frame, video_end_frame` | 该页画面的整数帧边界，尾端不含 |
| 页 `video_start, video_end, visual_offset_seconds` | 画面时间范围及其与音频起点的偏移 |
| 页 `source_video_duration` | 循环前的原页短片时长 |
| 句 `start,end` 与 `book_start,book_end` | 本页配音位置与全本音频位置；可由实际句 WAV 核对 |
| 句 `frame_start,frame_end,visual_boundary_crossed` | 原短片时间及是否跨画面边界；不适用时可为空 |

画面只能在整数帧切换，不能把音频浮点边界冒充精确画面边界。v2 记录量化差异，允许的量化尺度为一帧。映射仅在所属画面范围内成立：

```text
frame_time = (book_time - video_start) % source_video_duration
video_start <= book_time < video_end
```

页短片播放器知道 `frame_time`，却不自动知道整片 `book_time`。工作台只在「整本影片」模式下发送 `book_time`（整片播放器的 currentTime），页动画模式只发送 `frame_time`，不用短片时间伪造整片时间。

## 问答、引用与多媒体会话

QA JSON 必填 `question`（1至4000字），可选 `session_id,page_id,book_time,frame_time,audio_ref,image_ref`。`book_time` 仅在整片时钟真实可知时发送；服务端可依据画面时间轴定位页，引用同时保留音频及画面范围。

检索使用原文关键词/中文 bigram，以及有限历史中的指代线索。模型仅接检索证据与历史，必须以 `[p0001]` 等标记引用实际检索集合；缺引用或引用外部页会拒绝，仅返回实际被引用的 citations。引用合法性校验不是语义事实核验。

`answer_source`：

- `model_with_validated_citations`：模型页引用已校验。
- `model_abstained`：模型明确缺少依据，返回弃答。
- `no_evidence`：未找到足够原文证据，不调用模型。
- `extractive`：未配置问答模型，明确返回原文摘录。

检索在原文之外加入模型推断的画面词（`scene_analysis.data` 与 `objects.detections` 标签），权重为原文的一半：只能让无字页被找到，不能压过原文。命中时 citation 附 `visual: {inferred: true, terms}`，无字页 `excerpt` 为空串。

## 视频内检索 moments

`search` 与 `qa` 返回 `moments`：`{kind, page_id, page_num, text, start, end, book_start, book_end, inferred, score}`。

- `speech`：来自实测句子 WAV，`start/end` 为本页配音时间；整片导出后补 `book_start/book_end`。
- `text`：尚未配音的原文句，没有时间。
- `visual`：模型推断的画面命中，覆盖整页画面范围，`inferred=true`。

查询会先去掉「什么、哪里、时候」等提问用语，再按 bigram 匹配，当前页同分优先。前端点击 moment 时，有整片就跳到 `book_start` 播放，否则翻到该页并从句子 `start` 播放朗读。

citation 含 `page_id,page_num,excerpt,start,end,time_status`；导出可用后补 `book_start/book_end` 与 `video_start/video_end`。尚无导出时，时间范围为空并标明不可用，不能估算补齐。

session 落盘保存 `turns`，包含问题、回答、来源、引用、页与时间、相对媒体引用及创建时间。同书并发问答由读改写锁保护。客户端仅在 localStorage 保存 book/session IDs，正文与媒体历史从服务端恢复。

附件只接受本书合法、已存在的相对引用：图片支持 PNG/JPEG/WebP，音频支持 WAV/WebM/MP3/M4A/OGG/FLAC，并检查可解码性；禁止绝对和跨书路径。transcribe 保存音频后调用识别适配器，未配置返回503，不伪造文字。识别截图只有位于 `attachments/` 时才作为问题图片附件使用。

保存图片/语音引用不等于模型已理解其内容；当前答案主要检索绘本文字。QA 保存成功后，即使后续 speak 或浏览器播放失败，也应保留问答记录。完整边界见[功能状态](FEATURE_STATUS.md)与[验证记录](VALIDATION.md)。
