# Unitree Raw Capture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unitree 双相机独立、低负载、可恢复地保存原始像素，并保留快速分段和旧数据兼容。

**Architecture:** 常驻相机以订阅方式向每路有界队列交付图像，独立 writer 写 MCAP/LZ4；机器人和手柄 CSV 保持独立。离线 reader 统一处理新旧格式，恢复和转换不进入采集热路径。

**Tech Stack:** Unitree Linux ARM64 / Python 3.8 / OpenCV / NumPy / LZ4 / MCAP；Windows controller Python >=3.10 / pytest / 现有 HTML-JS 控制台。

**Spec:** `docs/superpowers/specs/2026-09-26-unitree-raw-capture-design.md`（用户于 2026-09-26 回复“继续”，批准设计进入计划阶段）。

## Global Constraints

- 每路队列按未压缩字节限制为 64 MiB，总像素排队上限 128 MiB。
- 队列达到 70% 发出积压警告；满队列不覆盖旧帧，触发当前记录任务收尾。
- 每路 MCAP 初始 chunk 目标 4 MiB、分片上限 512 MiB（允许超出最后一条记录的大小），开启 CRC。
- 写入线程每约 1 秒将已完成块 flush/fsync，Stop 再同步剩余内容。
- 启动低于 20 GiB 可用空间拒绝记录；运行低于 10 GiB 停止当前记录并排空有限队列，每秒检查。
- 正常 Stop 目标 p95 ≤ 3 秒（Unitree 单端），不是已经实现的承诺。
- 不改 P450 格式、Home 门控、遥操速度或对时阈值；不自动导出视频；不更改／删除旧数据。
- 无真实设备时间戳时标记 host_receive，不宣称传感器层零丢帧；同一 boot_id 内按 monotonic_ns 排序。
- 保留旧 recorder 显式回滚；配置默认值在实机验收前维持旧格式。部署时两端不得有活动采集。
- 当前工作区含大量已有修改和未跟踪代码，HEAD 不是完整基线。执行前依技能选择工作目录并保存范围清单；若使用隔离 checkout，必须保留这些相关文件，不可仅从 HEAD 重建后覆盖部署。

## Review Focus

1. 系统时间倒退而单调时钟正常：帧不覆盖、不因 wall_time 排序丢失（任务 1、6）。
2. Stop 与相机回调并发，旧回调延迟完成：不串段、不重用已关闭 writer（任务 3）。
3. 文件已写但 fsync／rename／目录同步失败：不提前标记 durable 或释放会话（任务 2、4）。
4. 新格式损坏且旁边有旧 PKL：明确拒绝，不静默回退伪造成功（任务 5、6）。
5. 远端故障与用户 Stop 同时发生、另一端 SSH 失联：幂等收尾，保留停止未确认，不影响运动控制（任务 7）。

## 文件与接口分工

新增 `remote/unitree/raw_image.py`（像素布局）、`mcap_storage.py`（持久化）、`raw_capture.py`（队列/订阅/分段）、`episode_io.py`（新旧读取/验收）、`recover_capture.py`（恢复 CLI）、`convert_episode.py`（离线转换）、`requirements-raw.txt`（验证后锁定依赖）。

修改 `hetero_pkl_recorder.py` 的 CameraReader 发布接口，保留旧 run；修改 `camera_session.py`、`collection_manager.py` 和 `validate_episode.py` 完成新格式编排。桌面修改 `inspectors.py`、`remote.py`、`console_state.py` 和 `console_web/app.js`，仅补充新格式支持和已有状态区，不重新设计整页布局。

测试放 `tests/test_raw_*.py`，沿用 `tests/test_warm_session.py` 将 `remote/unitree` 加入 sys.path 的方式。远端模块必须在 Python 3.8 真实编译/执行，Windows 测试不能替代该检查。

所有命令在 repo 根目录运行。每任务先新增指定测试，执行并确认它因缺失功能失败，再实现、重跑。每任务做范围 diff 审查；只提交本任务新增文件及确认过的修改片段，禁止 `git add .` 将既有修改捎带提交。下面“范围提交”均遵循此规则。

### Task 1: 版本化像素记录与依赖兼容

**Files:** Create `remote/unitree/raw_image.py`, `remote/unitree/requirements-raw.txt`; Test `tests/test_raw_image.py`。

**Interfaces:** `encode_image(metadata: dict, image: ndarray) -> bytes`; `decode_image(payload: bytes) -> tuple[dict, ndarray]`。payload 为 uint32 little-endian JSON 长度、UTF-8 metadata、连续 BGR8 字节；布局名 `heterovla.raw_image.v1`。schema 文档由 `image_schema() -> dict` 提供。

- [ ] 写 `test_pixels_round_trip`（随机 BGR8 和非连续切片还原相同像素）、`test_integer_ns_survives_roundtrip`（大于 2**53 的 ns 整数不变）、`test_rejects_malformed_size`（截断、过长 JSON、负尺寸、非 uint8、stride/像素长度不一致均 ValueError）、`test_wall_clock_step_does_not_change_sequence`。接收上限 metadata=64 KiB，单条 payload=64 MiB；本次只支持配置指定 BGR8 尺寸，不静默 resize。
- [ ] 执行 `python -m pytest -q tests/test_raw_image.py`，确认预期失败。
- [ ] 实现上述签名和必需元数据验证；JSON schema 说明与实际布局一致，不宣称标准 ROS/Foxglove 编码。
- [ ] 在独立测试环境查询 MCAP/LZ4 官方 API 与 Python 要求，选择可在 Unitree Python 3.8/ARM64 使用的发布版本，写精确版本锁定。验证标准 public writer 的 chunk 提交、CRC、reader 截断行为；不改系统环境、不猜版本、不使用未测试私有 API。若无法满足周期持久化，停止并报告具体阻碍。
- [ ] 重跑测试、Unitree Python 3.8 编译；范围提交 `feat: define versioned raw image records`。

### Task 2: MCAP 顺序写入与持久化

**Files:** Create `remote/unitree/mcap_storage.py`; Test `tests/test_raw_storage.py`。

**Interfaces:** `CameraStore(directory: Path, camera: str, codec: str, *, clock, io_hooks)`；`append(metadata: dict, image: ndarray) -> None`、`checkpoint() -> dict`、`close() -> dict`、`stats() -> dict`。消费任务 1 的像素编码；返回计数 written/durable、bytes、各分片时间范围和错误。单一 writer 线程调用写接口。

- [ ] 写 `test_crc_roundtrip_and_rotation`（两路分片独立、像素相等）、`test_durable_advances_only_after_fsync`、`test_failed_rename_keeps_active_file`、`test_checkpoint_without_new_frame`、`test_existing_file_never_overwritten`。通过 io_hooks 注入 ENOSPC/EIO 和同步失败；断言异常可见、manifest 不虚报成功。
- [ ] 执行 `python -m pytest -q tests/test_raw_storage.py`，确认失败。
- [ ] 实现公共 MCAP API 封装，4 MiB chunk、512 MiB 轮换、CRC、`.active` 到正式文件的安全发布。计数区分 API 接受记录、已提交块和 fsync 确认，低帧率不足一块时不得虚报 durable。周期 checkpoint 同步所有已提交块，close 完成剩余块/footer。
- [ ] 实现显式 codec `lz4`/`none`，不自动切换；原子 manifest 更新并同步目录；保持每路序号而非 wall_time 文件命名。
- [ ] 重跑测试，范围提交 `feat: persist camera streams in durable mcap shards`。

### Task 3: 原始帧订阅与有界队列

**Files:** Create `remote/unitree/raw_capture.py`; Modify `remote/unitree/hetero_pkl_recorder.py: CameraReader`; Test `tests/test_raw_capture.py`。

**Interfaces:** CameraReader 增加 `subscribe(callback) -> str`、`unsubscribe(token: str) -> int`；后者返回截止 reader_seq，保证返回后不会再向该订阅提交帧。`RawCapture(episode_dir, config, cameras, *, store_factory=CameraStore)` 提供 `start() -> None`、`request_stop() -> None`、`wait_stopped(timeout: float) -> bool`、`status() -> dict`。内部每相机 FIFO 限制未压缩像素字节。

- [ ] 写 `test_front_saved_without_wrist_or_state`、`test_no_latest_replay_at_start`、`test_two_episodes_have_disjoint_sequences`、`test_queue_full_preserves_old_frames_and_faults`、`test_unsubscribe_race_has_no_late_append`、`test_camera_buffer_mutation_cannot_corrupt_saved_frame`。64 MiB 上限、70% 警告精确断言；用 barrier 而非 sleep 驱动并发测试。
- [ ] 执行 `python -m pytest -q tests/test_raw_capture.py`，确认失败。
- [ ] 实现订阅热路径：短锁内确定本段归属、复制或转交已证明不可变的图像、非阻塞入队；锁内无编码/fsync/join。保留旧 snapshot 功能，不启动第二个相机所有者。
- [ ] 实现两 writer 独立排空，1 秒 checkpoint/磁盘检查；received/accepted/written/rejected/write_errors/pending 和 per-episode seq 语义独立。容量阈值、相机 error 或 >500 ms 无新帧导致记录故障与 request_stop，不调用运动控制。
- [ ] 重跑新测试及 `python -m pytest -q tests/test_warm_session.py`；范围提交 `feat: capture independent camera frames with bounded queues`。

### Task 4: 分段编排与可响应的停止

**Files:** Modify `remote/unitree/camera_session.py`, `remote/unitree/collection_manager.py`, `remote/unitree/session_support.py`; Test `tests/test_raw_session.py`, existing `tests/test_warm_session.py`。

**Interfaces:** CameraSession 原 start/stop/status 外部操作名不变；format_version=2 使用 RawCapture。新增 v2 status：`streams.front/wrist`、`recording_state`、`quality_ok`、`fault`、`durable_complete`；v1 维持原字段。不把独立相机帧数叫“双相机组数”。

- [ ] 写 `test_start_requires_both_streams_and_states_but_does_not_gate_image_writes`、`test_status_remains_responsive_during_stop`、`test_stop_timeout_retains_episode`、`test_concurrent_stop_is_idempotent`、`test_stop_ack_requires_csv_and_image_durability`、`test_low_disk_refuses_before_subscription`。慢盘模拟下 status 在 250 ms 内返回，Stop 按既有超时契约失败但可重试。
- [ ] 执行 `python -m pytest -q tests/test_raw_session.py`，确认失败。
- [ ] 分离状态请求与耗时 wait_stopped；操作锁保护生命周期，status 仅读缓存快照。Stop 请求固定图像边界，排空后关闭 Piper CSV，再取消分段手柄 CSV、结束 Go2 bridge；补充本段状态文件的 fsync 确认，不能只 flush 就宣称落盘。
- [ ] 元数据写 format_version/image_storage/codec/boot_id/config/software，`capture_manifest.json` 保留缺失结束标记的恢复线索；故障也可完成收尾但 quality_ok=false。配置默认仍为旧 recorder，新模式仅测试配置启用。
- [ ] 重跑上述测试及旧 fast-stop/warm-session 测试；范围提交 `feat: integrate raw capture with warm episode lifecycle`。

### Task 5: 统一读取、完整性验收与非破坏恢复

**Files:** Create `remote/unitree/episode_io.py`, `remote/unitree/recover_capture.py`; Modify `remote/unitree/validate_episode.py`; Test `tests/test_raw_recovery.py`, `tests/test_raw_reader.py`, existing `tests/test_unitree_validator.py`。

**Interfaces:** `iter_camera_frames(episode: Path, camera: str) -> Iterator[tuple[dict, ndarray]]`（v1/v2）；`inspect_episode(episode: Path) -> dict`；`recover_episode(source: Path, destination: Path) -> dict`。reader 单调时钟序按分片/seq 读取，不依赖 MCAP wall-time 索引排序。恢复 CLI `python recover_capture.py --episode SOURCE --output NEW_DIRECTORY`。

- [ ] 写 `test_v1_and_v2_read_same_pixels`、`test_corrupt_v2_never_falls_back_to_pkl`、`test_truncated_tail_recovers_complete_chunks_only`、`test_crc_failure_reports_loss`、`test_recovery_never_modifies_source`、`test_boot_change_rejected_within_episode`、`test_counts_include_queue_rejections`。检查恢复前后源文件 hash 相同，未知损失上界明确标记 unknown。
- [ ] 执行 `python -m pytest -q tests/test_raw_reader.py tests/test_raw_recovery.py`，确认失败。
- [ ] 实现格式显式分派、逐记录长度/CRC/维度/时间/序号/计数检查；区分 raw_closed、application_integrity、alignment_valid、sensor_integrity_unknown。恢复只复制可验证完整记录至新文件，保留故障报告，不改源或删异常帧。
- [ ] 更新 validate_episode 分派；新数据不要求 frames/*.pkl；保留旧验收入口和既有质量阈值。raw_closed 不能替代训练数据有效性。
- [ ] 重跑新测试和旧 validator 测试，范围提交 `feat: validate and recover versioned raw episodes`。

### Task 6: 离线转换与训练兼容

**Files:** Create `remote/unitree/convert_episode.py`; Test `tests/test_raw_conversion.py`。

**Interfaces:** `convert_episode(source: Path, output: Path, *, target: str, mapping: Path | None, config: dict) -> dict`；CLI target 支持 `pkl`/`video`，默认无副作用（只有显式执行才转换）。输出目录不得覆盖源或已有结果。

- [ ] 写 `test_png_pkl_roundtrip_matches_raw_pixels`、`test_asymmetric_rates_use_unique_nearest_frames`、`test_wall_jump_pairs_by_monotonic`、`test_missing_state_or_home_enable_rejects_training_sample`、`test_invalid_clock_mapping_does_not_claim_alignment`、`test_video_export_keeps_timestamp_sidecar`。
- [ ] 执行 `python -m pytest -q tests/test_raw_conversion.py`，确认失败。
- [ ] 用任务 5 reader，以 front 为基准按 monotonic_ns 最近邻配对 wrist（不重用 wrist，等距选较早），应用现有 max_camera_skew_ms 和状态 stale 阈值；用原始 CSV 重建旧记录字段。匹配失败只记录报告，不删原始流、不插值伪造状态。
- [ ] 旧 PKL 输出保留原 wall/mono ns 与配对误差，PNG 编码仅离线执行；视频可各路独立导出并配 sidecar。无可靠跨机 mapping 时可生成标为未对齐的预览，禁止标为联合训练就绪。
- [ ] 重跑测试并对固定小样本逐像素验证；范围提交 `feat: convert raw episodes offline without mutating sources`。

### Task 7: 联合控制器、检查器与看板适配

**Files:** Modify `src/jointctl/inspectors.py`, `src/jointctl/remote.py`, `src/jointctl/console_state.py`, `src/jointctl/console_web/app.js`, `src/jointctl/console_web/index.html`（仅状态元素）；必要时修改 `src/jointctl/controller.py` 的既有故障收尾入口；Test `tests/test_raw_controller.py`, `tests/test_console_state.py`。

**Interfaces:** 保持 `UnitreeInspector.summarize(session_dir) -> list[StreamSummary]`；v2 远端调用 episode_io 的只读汇总，不现场 decode 全图；完整验收仍显式离线执行。RemoteStatus 保留两流 quality/fault/raw_closed；旧消费者有版本分派，不伪造旧 frames_saved。

- [ ] 写 `test_start_accepts_two_v2_streams_without_pkl`、`test_fault_stops_joint_episode_once`、`test_disconnected_peer_remains_unconfirmed`、`test_user_stop_race_is_idempotent`、`test_raw_closed_is_not_alignment_pass`、`test_ns_fields_not_roundtripped_as_js_numbers`。counter/queue/disk估算 UI 呈现测试并覆盖 v1。
- [ ] 执行 `python -m pytest -q tests/test_raw_controller.py tests/test_console_state.py`，确认新测试失败。
- [ ] 使用现有操作锁和 Stop/rollback 路径执行故障收尾，status GET 保持只读；后台监测明确错误时幂等提交收尾，不为一次 SSH 超时重复发起 Stop，不让另一端继续显示共同采集正常。
- [ ] 在现有上方设备区显示各相机接收/保存 FPS、队列字节占比、错误数、写入速率和按无压缩速率估算的可录分钟；分开展示收尾/质量/对时状态。不改整页布局或增加自动导出。
- [ ] 重跑 `python -m pytest -q tests`，浏览器验证运行/积压/停止/失联四态；范围提交 `feat: expose raw-stream health in joint capture console`。

### Task 8: 基准、实机验收、发布与回滚

**Files:** Create `scripts/benchmark_unitree_raw.py`, `docs/2026-09-26-unitree-raw-capture-verification.md`; deployment updates only approved Unitree modules/dependencies/config and desktop controller copies。

**Interfaces:** benchmark CLI `--output NEW_DIRECTORY --seconds 120 --codec lz4|none`，仅新增测试目录，打印 JSON throughput/latency/cpu/rss/fsync/queue，不自动删除。生成不可压缩及实际样本两种负载，防止重复静态帧使压缩率虚高。

- [ ] 执行新旧全套本地测试、Unitree Python 3.8 编译及隔离测试环境依赖加载；保存版本/hash/基线。若真机有活动采集，不停止用户任务，不部署。
- [ ] 运行 120 秒包含周期 fsync 的持续写入基准，吞吐需 ≥2×(55.3 MB/s + 实测状态写入速率)，记录延迟分位数。失败则保留旧默认并报告瓶颈，不通过放大队列或放宽指标隐藏。
- [ ] 备份真机当前模块/配置/依赖清单；单独测试配置启用 v2。静止录制 10 分钟，要求应用层 rejected/write_errors=0、received=accepted=written，全部记录可读、CRC通过；记录帧间隔分位数、FPS、CPU/RSS。CPU平均<可用四核总容量70%，队列无持续增长。
- [ ] 连续 10 次短段，检查相机/手柄 PID 不变，无串段，Stop p95≤3秒；慢盘模拟保留清晰 pending 状态。不按 Home、不使能或移动机器人。
- [ ] 在独立测试进程制造中断，验证任务5恢复；不切断真机电源，不杀实际相机常驻服务，不删除测试源。明确不能恢复的尾部范围。
- [ ] 双机短录制回归：P450 bag 不变、v2 被识别、时钟映射按原规则通过才报告对齐；对时若仍失败，独立记录，不借本次升级放宽阈值。手动运行离线转换验证像素与状态，然后确认普通 Stop 未产生视频。
- [ ] 通过后才切换生产默认至 v2，部署版本/hash一致，空闲时重启受影响相机服务；再做一次短段回归。失败则恢复备份模块/配置及兼容依赖，不改旧数据，保留测试证据。
- [ ] 文档记录命令、结果、文件路径、旧数据读取方法、恢复和手动导出方法；列出需同时转移的对时文件。范围提交 `test: verify and deploy Unitree raw capture`。

## 自审与交付顺序

Spec §1–2：任务1/8；§3–4：任务1–4；§5–6：任务2–5/7；§7：任务5–7；§8：任务8。Review Focus 五项均已分配明确测试。生产切换晚于读取、恢复、转换和联合控制器适配，不会出现“已经换格式但没有恢复工具”的交付状态。

建议本会话由主代理顺序执行：各任务共享时间戳、生命周期及格式契约，顺序实现便于统一调试；完成后独立审查整个修改。另一选项为逐任务子代理实现与审查，隔离审查更充分但上下文开销更大。等待用户审阅本计划并选择执行方式，当前所有实施项未开始。
