# Unitree 系统盘维护方案

已确认系统根分区是 `/dev/nvme0n1p1`，UUID `7073f027-1f76-471e-8386-b7ac9f58e0e6`，当前以读写方式挂载于 `/`。2026-09-27 读取 superblock 仍为 `clean with errors`，错误计数 82，函数 `ext4_validate_block_bitmap`。历史计数不是损坏文件数量；时钟不可靠，不能依赖机载日期判断错误发生顺序。未证明 SSD 物理损坏或它导致了 SSH 失联。

## 在进行修复前

1. 停止采集，确认两端录制进程结束。将 Unitree `/home/unitree/heterovla-data/datasets`、`/home/unitree/heterovla-collection` 和需要保留的控制代码复制到另一块健康磁盘，并校验重要文件。Windows 的 `D:/OneDriveData/Desktop/joint_manifests` 和 P450 对应目录也必须保留。
2. 若读取出现 I/O 错误或频繁掉盘，先停止反复扫描，让具备恢复条件的人员做磁盘镜像；不要先反复修复原盘。
3. 从兼容的维护系统启动 Unitree，或安全关机、断电后拆下 NVMe 接到另一台 Linux 电脑。不要假设换机后设备仍叫 nvme0n1p1；使用上面的 UUID 和磁盘容量确认。
4. 在维护系统中检查 `lsblk -f` 和 `findmnt`，确认该 UUID 对应分区没有任何挂载点。不要在当前正在运行的根分区上执行修复，也不要把只读扫描挂载中分区的结果当可靠诊断。

## 离线检查与修复

以下为维护人员在备份完成且目标分区未挂载后执行的步骤，本次未执行：

```bash
# 先根据 UUID 核实目标，输出应指向已确认的 EXT4 分区。
readlink -f /dev/disk/by-uuid/7073f027-1f76-471e-8386-b7ac9f58e0e6
lsblk -f

# 未挂载状态下先完整检查、记录结果，不修改。
sudo e2fsck -f -n /dev/disk/by-uuid/7073f027-1f76-471e-8386-b7ac9f58e0e6

# 确认备份及检查结果后，交互式修复；逐项审阅提示，不盲目 -y。
sudo e2fsck -f /dev/disk/by-uuid/7073f027-1f76-471e-8386-b7ac9f58e0e6

# 修复后再次检查，应报告无待修复错误。
sudo e2fsck -f -n /dev/disk/by-uuid/7073f027-1f76-471e-8386-b7ac9f58e0e6
```

修复可能重建位图、将无法关联目录的文件放入 lost+found；它不能恢复所有损坏的数据。不要通过清空错误计数或关闭 metadata_csum 来代替修复。

## 恢复采集前

- 检查 SSD SMART/NVMe 健康和错误日志；当前机载环境未发现 smartctl/nvme 工具，未取得硬件健康报告。
- 重启后复核内核无新 EXT4/NVMe I/O 错误，做受控写入、fsync、读回哈希及分段传输测试。
- 在原始 v2 格式下完成无动作短段、连续分段、代表性长段验收。检查图像计数、状态覆盖、容器 CRC、停止持久化以及时钟映射。
- 若错误复发，继续检查供电、NVMe 接触与散热、PCIe 链路或更换 SSD；不要仅再次清除标记。

参考：[e2fsck 手册](https://man7.org/linux/man-pages/man8/e2fsck.8.html)、[Linux EXT4 元数据校验文档](https://docs.kernel.org/filesystems/ext4/checksums.html)。
