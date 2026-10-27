# VideoRoll 原生部署：Redis AOF 安全恢复

Redis 启用 AOF everysec 持久化，但意外断电或虚拟机异常重启仍可能损坏尾部增量 AOF。VideoRoll 启动前执行预检，避免 Redis 反复启动失败导致 API 长时间返回 502。

## 自动恢复的边界

- Redis 未运行时，预检使用同版本 redis-check-aof 检验多段 AOF。
- 只接受基础 AOF 有效、且最后一个 INCR 文件尾部损坏的情形。
- 必须能精确解析有效偏移；自动修复上限为 4,096 字节。不符合条件时拒绝启动并保留原数据。
- 修复前复制整个 redis-native 数据目录，保存 SHA-256 校验和并同步备份文件到磁盘；修复后再次执行 AOF 校验。
- 自动备份位于 Redis 数据目录同级的 redis-aof-autorecovery 中。此备份与原始数据位于同一卷，不能替代离线/异地备份；不会自动删除。
- 修复可能丢失损坏处的部分近期写入。PostgreSQL / Hatchet 工作流数据与发布资产仍需幂等状态对账。

Redis 由 systemd 在启动前执行预检、启动后等待 PONG，并以非阻塞方式恢复应运行的 VideoRoll Worker。Redis 多次连续启动失败会触发 systemd 启动频率限制，避免无限重启。

## 运维建议

- 定期将 Redis、PostgreSQL 及资源目录备份到不同物理存储设备；根据恢复点要求决定备份频率。
- 如果自动恢复拒绝修复，应先检查 journalctl -u videoroll-redis -b，再备份数据，按 Redis 对应版本的工具输出估计损失，切勿直接删除 AOF 或整个 Redis 目录。
- 如果损坏超出 4 KiB 或涉及基础/中间 AOF，必须进行人工核查。不要提高阈值让系统盲目截断。
- 通过 systemctl status 和 VideoRoll API 健康检查确认恢复；再对照 PostgreSQL 和 Hatchet 任务记录检查中断与重复提交。
- 检查 PVE/KVM 宿主机异常重启原因。AOF 自动修复无法解决宿主机供电、存储或内核故障。

脚本与 systemd 单元随 scripts/native/install.sh 安装；既有生产环境可以用 systemd drop-in，无需重新部署 Python 应用。
