# Unitree 原始双相机采集验证（2026-09-26）

状态：隔离验收进行中，**生产默认仍为旧格式**。不得把以下单项通过理解为整套系统已经发布或联合对时已通过。

## 实现范围

- v2 独立保存前置、腕部 BGR8 图像及主机接收时刻，以 MCAP + LZ4 无损写盘；不在采集中配对或编码 PNG/视频。
- 保留 Go2、Piper、手柄状态 CSV，以及时钟和会话元数据。手柄 CSV 增加六个笛卡尔目标字段，不改变控制和 Home 逻辑。
- Stop 取消本段订阅、排空有限队列、关闭 MCAP 并确认状态 CSV 落盘。没有自动导出视频。
- 接收、接纳、提交、写入、持久化分别计数。队列满、写错、相机停帧不能悄悄当成功。
- 桌面 v2 就绪要求两路都持续增长；后台明确故障只触发一次当前会话收尾。SSH 暂时失联不被误判为已经停止。
- 看板分别呈现原始文件收尾、数据质量和对时验收。小屏仍需滚动到完整操作区。

## 已取得证据

2026-09-26 22:02–22:05 本机时间，使用已安装桌面程序完成正式双机初始化及两条无动作短采集。第一条 `joint_20260926_220250_UTCp0800_f32d`：P450 bag 28,942,492 字节，Unitree 原始 PKL/CSV 保留；双端 Start/Stop 均返回成功，对时相对不确定度 3.906 ms。第二条 `joint_20260926_220527_UTCp0800_8624`：**未手动再初始化**即可再次开始和停止，目录均直接使用本机 `20260926_HHMMSS_UTCp0800` 名称。手柄曾连接、相机两路就绪；测试没有按 Home 或发运动指令。两条 Stop 后均可继续 Start，但数据验收单独进行。

这两条生产旧格式暴露关键问题：Unitree 第一条 `frames_enqueued=1962`、`frames_saved=1717`、`frames_dropped=245`，第二条 `1213/916/297`；旧验收器原先只看保存文件和平均频率，曾把第一条错误标为通过。现在 RED→GREEN 修正旧格式验收器、远程 inspector、联合报告与看板：明确报告丢帧，不再把对时精度与数据完整性混同。对第一条显式重验后 `postprocess=failed`，重新生成的 `alignment.json` 为 `data_validated=false`、`valid_interval=null`，保留独立对时估计 3.906 ms；第二条同样 `postprocess=failed`。**这两条不得直接当完整训练数据。** 原始文件与时钟记录不删除，亦不伪造丢失的帧。桌面看板已改为按采集创建时间而非重验修改时间显示最近一条，并直接显示失败原因；旧格式录制中丢帧会撤销绿色 LIVE 提示。

Unitree USB 接收器在本次运行期间多次在 `3537:1041`（有 `/dev/input/js0`）和 `3537:2106`（无 js 设备）之间重枚举。用户确认手柄曾恢复，随后又切到 `2106`；属于实际设备/配对模式变化，不是 SSH 或网页缓存误报。检查当前常驻遥操代码发现游戏手柄消失时上游 `update()` 直接返回，而包装层仍会依照保留的 `arm_enabled` 状态重复发送旧目标。本地已添加断连时不发送新指令且快照标无效的测试和守卫，**未在正在使能的机械臂上重启或部署该控制进程**；生产遥操仍是旧版，现场停止使用并维护时才能安全切换和验证。

截至该轮本地全套回归为 361 passed in 43.03s，另有 Chrome 真正渲染页面的失败原因/录制中丢帧两项测试；P450 实机原有完整测试 75 passed。Unitree v2 生产默认和系统环境未切换，历史 EXT4 错误及大文件传输时整机失联仍是发布阻碍。断连遥操保护仅在隔离脚本中经过 Unitree Python 3.8 编译，生产进程尚未切换。

本地完整回归最近一次：348 passed in 34.24s。包含实际 ffmpeg 导出合成小样本、逐像素恢复、CRC/截断、写入失败、并发停止和旧格式回归。最终审查三项 Important 已分别用失败测试复现并修正：最终 metadata/目录同步、MCAP summary CRC、停止后的故障与落盘计数留存。最新版 Python 3.8 隔离模块编译通过。

实机环境：Python 3.8.10、MCAP 1.3.0、LZ4 3.0.2+dfsg、OpenCV 4.2.0。MCAP 仅复制到隔离目录，没有替换系统环境。

隔离根目录：

`/home/unitree/heterovla-collection/.raw-capture-test-20260926`

120 秒双写线程基准，每种负载 60 秒，周期 fsync 且包含最终关闭时间：

| 负载 | 原始字节吞吐 | CPU（四核总容量） | fsync p95 | 写入错误 |
| --- | ---: | ---: | ---: | ---: |
| 不可压缩图像 | 228.39 MB/s | 41.35% | 174.51 ms | 0 |
| 真相机样本 | 201.65 MB/s | 43.00% | 126.89 ms | 0 |

门槛 112.69 MB/s，为双路 640×480×3×30 加 1 MiB/s 状态预算的两倍。状态预算目前为保守估算，完整采集后还要核对实际速率。基准不是实际相机不丢帧的证明。

证据：`benchmark-20260926-2004/result.json`。测试输出保留，不自动删除。

独立进程异常退出测试：`interruption-fkkrmnlp/verification.json`。两路各恢复 8 条完整记录，像素与序号一致；源文件哈希不变，尾部损失上界报告 unknown。没有杀相机服务或断电。

10 分钟实际采集/连续分段测试根目录：`integration-626u5yoy`。过程数据见 `hardware_verification.json`、`process_resources.json` 和每条 `hardware_samples.json`。

- 静止记录 601.286 秒，每路 received=written=durable=18024；rejected/write_errors/state_gap_ticks=0。有效帧率约 29.98 FPS；最大间隔前置 73.664 ms、腕部 73.896 ms。
- 其后 10 次短段均通过当时的图像计数和 chunk/data CRC 检查；相机与手柄 PID 未变。11 次停止 p95=0.539 秒，长段停止 0.330 秒。
- 上述硬件检查早于 summary CRC 修正，不称为最终完整验收。状态 CSV 全量验收、完整间隔分位数、最终代码实机短段仍待完成。
- 后续用修正后的完整容器/CSV 验收器读完上述 10 分钟数据，MCAP CRC、帧序号、像素和相机计数均通过，但发现 `piper_state.csv` 比首帧约晚 0.884 秒；该历史段整体 `application_integrity=false`，需要离线裁去未覆盖前缀，不能标为整段训练可用。根因是每段 PiperReader 要等 CAN 接收线程 0.5 秒才开始写 CSV，而相机先订阅。之后调整为先等本段 PiPER 首个状态（最长 1.5 秒，故障时仍保留图像）再订阅相机。
- 最新隔离复测 `review_regression_20260926`：两路各 187 帧，received=accepted=written=durable，rejected/write_errors=0；完整 MCAP+CSV 验收 `application_integrity=true`、`errors=[]`，PiPER 第一行早于相机首帧，Stop 0.379 秒，未按 Home 或发运动指令。原始数据仍在 `integration-626u5yoy/data/raw_hardware_test/review_regression_20260926`，完整报告 `integration-626u5yoy/review_regression_result.json`。
- 双机短段 `joint_20260926_122446_e04c` 启停成功；原对时阈值未修改，报告 data_validated=true，relative uncertainty=3.402235 ms。没有自动视频导出。

后续复制实机短段时 Unitree 断网，SCP 失败。一次恢复连接显示 uptime 约 496 秒且无活动采集进程，表明期间重启。用户重启 Unitree、禁用自动休眠后，新版隔离短段与完整校验通过；但再次 SCP 约传输 49 MB 时 SSH 连接关闭，之后 SSH 超时、ARP incomplete。本机 Realtek USB 网口仍显示 1 Gbps Up。故不能把此前断网归因于自动休眠；需要先查机载电脑是否因传输负载重启或网卡链路异常。本地 `segment_10`、`segment_10_complete`、`review_regression_20260926_local`、`offline-video-test` 均是未验收的部分输出，不得作为完整样本使用。旧生产默认未修改；不得把本地软件测试通过当作生产已发布。

用户再次重启后，Unitree boot ID 变为 `aa873fdd-ab51-4256-a655-1f1a0765d6e0`。其系统盘 `/dev/nvme0n1p1` 的 EXT4 superblock 为 `clean with errors`、FS Error count=82，最后一次错误为 2026-09-24 13:19:47（机载系统时钟不可靠），类型 `ext4_validate_block_bitmap`；本次启动内核明确提示 `mounting fs with errors, running e2fsck is recommended`。这证明存在需要离线检查的历史文件系统错误，但不是本次传输断连的直接证据。前一启动 syslog 在桌面 portal 超时后突然结束，没有网卡 down、NVMe I/O 错误、OOM、panic 或正常关机记录；pstore 为空，因此不能在网卡、供电、内核挂死之间精确归因。当前启动内存约 13 GiB available、NVMe 状态 live、网卡 1 Gbps full duplex 且错误计数 0；本机读取问题 MCAP 前 64 MiB 用 0.114 秒完成，SSH 读取 `/dev/zero` 的 4 MiB 和 16 MiB 均成功。没有再次触发 49 MiB 以上全量传输，以免再次失联。**在备份数据、离线 fsck 与现场电源/网口状态确认前，不宣布硬件稳定，也不切换生产格式。**

## 离线读取、恢复、导出

这些入口使用 v2 模块目录中的 Python，需能导入 mcap、lz4、numpy，视频导出另需 ffmpeg。旧 v1 PKL 保持可读；只对可信来源的 PKL 使用 Python pickle。

```bash
# 非破坏恢复：输出必须是不存在且不位于源目录内的新路径
python3 recover_capture.py --episode /data/source --output /data/recovered-new

# 手动预览视频：原始纳秒时间保存在 timestamps.csv；MP4 本身是固定帧率预览
python3 convert_episode.py --episode /data/source --output /data/video-new \
  --target video --config /path/to/collection.json

# 离线配对并重建 PKL：没有有效 mapping/Home/状态条件时不得声称训练就绪
python3 convert_episode.py --episode /data/source --output /data/pkl-new \
  --target pkl --config /path/to/collection.json --mapping /archive/alignment.json
```

恢复或转换都不能修复相机尚未送达应用层的帧。图像时间是主机接收时刻，不是硬件同步曝光时间。原始数据完整与联合训练可用是两项独立验收。

## 迁移数据时一起保存

1. Unitree 整个 episode 目录，包括 `raw/cameras/*.mcap`、schema、全部状态 CSV/JSON、meta、summary、capture_manifest。
2. P450 整个对应采集目录，包括原始 bag 和元数据。
3. Windows 对应 `joint_manifests/<episode_id>/` 的全部内容，包括 manifest、clock 样本、monitor 配置与 alignment 报告；不要只复制视频。

失败的数据同样保留；不要靠删除异常帧或修改质量标签把失败变成通过。

## 尚待发布门槛

- 查明传输约 49 MB 时 Unitree 消失的电源／网卡原因；复核状态稳定并分段传输、校验哈希。
- 长段的历史 PiPER 前缀必须明确标记或在离线样本中剔除；新版短段完整验证已通过。补齐 CPU/队列统计。
- 下载完整实机短段后验证显式离线转换（合成样本已通过；实机样本尚未完成）。
- 通过后备份生产模块/配置，再切换格式；否则保留旧默认。
