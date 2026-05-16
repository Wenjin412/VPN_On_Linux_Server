# VPN On Linux Server

一个面向 Linux 服务器的轻量 VPN/代理管理工具。底层使用成熟的
[Mihomo](https://github.com/MetaCubeX/mihomo) 内核，本项目提供一条简单的
`vpncli` 命令来完成安装、订阅管理、节点切换、自动测速选点、systemd 开机自启动和健康检查。

## 设计目标

- 简单安装：一条命令安装 Mihomo 内核、`vpncli`、systemd 服务。
- 任意目录可用：安装后全局命令为 `vpncli`。
- 订阅管理：支持 Clash/Mihomo 订阅链接，支持刷新、替换、隐藏显示。
- 节点管理：列出节点、按序号/名称切换节点、自动选择最佳节点。
- 面向常用 AI 产品优化：自动选点默认测试 Google、OpenAI、Anthropic 连接质量。
- 保护服务器入站服务：默认只监听 `127.0.0.1` 本机代理端口，不修改系统路由、不开放局域网代理。

## 快速安装

先把订阅链接放到变量里，注意不要照抄尖括号占位符：

```bash
export SUB_URL='https://example.com/your/subscription?clash=1'
```

```bash
curl -fsSL https://raw.githubusercontent.com/Wenjin412/VPN_On_Linux_Server/main/scripts/install.sh \
  | sudo bash -s -- --subscription "$SUB_URL"
```

大陆服务器如果访问 GitHub 很慢，可以使用自己信任的 GitHub 加速/反代前缀。安装器会把 GitHub
release、raw 文件都通过这个前缀重试下载：

```bash
export VPNCLI_GITHUB_PROXY='<your-trusted-github-proxy-prefix>'
curl -fsSL "${VPNCLI_GITHUB_PROXY%/}/https://raw.githubusercontent.com/Wenjin412/VPN_On_Linux_Server/main/scripts/install.sh" \
  | sudo VPNCLI_GITHUB_PROXY="$VPNCLI_GITHUB_PROXY" bash -s -- --subscription "$SUB_URL"
```

`VPNCLI_GITHUB_PROXY` 支持两种格式：

- `https://mirror.example.com/` 会拼成 `https://mirror.example.com/https://github.com/...`
- `https://mirror.example.com/?url={url}` 会把 `{url}` 替换成完整 GitHub URL

如果想先安装再配置：

```bash
curl -fsSL https://raw.githubusercontent.com/Wenjin412/VPN_On_Linux_Server/main/scripts/install.sh | sudo bash
sudo vpncli setup "$SUB_URL"
```

安装后服务会通过 systemd 开机自启动：

```bash
vpncli status
vpncli test
```

## 常用命令

```bash
# 启动/停止/重启
sudo vpncli start
sudo vpncli stop
sudo vpncli restart

# 管理订阅
sudo vpncli subscription show
sudo vpncli subscription set '<new-subscription-url>'
sudo vpncli subscription refresh
sudo vpncli subscription tls-verify auto  # on/off/auto

# 节点管理
vpncli nodes        # 交互式列出全部节点并提示选择
vpncli nodes list
sudo vpncli nodes use 3
sudo vpncli nodes use 'Hong Kong'
sudo vpncli auto

# 测试当前节点能否访问目标产品
vpncli test
vpncli test google openai anthropic

# 给单个命令临时走代理
vpncli run -- curl -I https://api.openai.com/v1/models

# 输出当前 shell 的代理环境变量
eval "$(vpncli env)"
```

## 路由模式

默认是 `targeted`：

- Google / OpenAI / Anthropic 相关域名走 VPN。
- 其他流量直连。
- 不修改系统默认路由，因此不会影响服务器对外提供的 API 入站连接。

切换为全局代理客户端模式：

```bash
sudo vpncli mode global
sudo vpncli restart
```

切回默认模式：

```bash
sudo vpncli mode targeted
sudo vpncli restart
```

## TUN 透明代理

默认关闭 TUN。大多数服务器场景建议保持关闭，只让需要代理的命令或程序使用
`http://127.0.0.1:7890`。

如确实需要透明代理整个服务器的出站流量：

```bash
sudo vpncli tun enable
sudo vpncli restart
```

关闭：

```bash
sudo vpncli tun disable
sudo vpncli restart
```

启用 TUN 会修改服务器出站路由，请在有回滚通道的情况下操作，并观察线上 API 服务。

## 文件位置

- 命令：`/usr/local/bin/vpncli`
- 程序目录：`/opt/vpn-on-linux`
- 配置：`/etc/vpn-on-linux`
- 订阅缓存：`/etc/vpn-on-linux/providers/subscription.yaml`
- systemd unit：`/etc/systemd/system/vpn-on-linux.service`

配置文件权限默认收紧到 `0600/0700`，订阅链接不会在命令输出中明文展示。

`vpnctl` 仍会作为兼容别名安装，但新文档和新服务统一使用 `vpncli`。

## 卸载

```bash
sudo bash scripts/uninstall.sh
```

连配置一起清理：

```bash
sudo bash scripts/uninstall.sh --purge
```

## 开发测试

单元测试：

```bash
python3 -m unittest discover -s tests -v
```

本地冒烟测试会下载当前平台的 Mihomo，使用临时端口和临时目录，不会修改系统路由：

```bash
VPN_SUBSCRIPTION_URL='<your-test-subscription-url>' bash scripts/dev_smoke_test.sh
```

包含自动选点的冒烟测试：

```bash
VPN_SUBSCRIPTION_URL='<your-test-subscription-url>' bash scripts/dev_smoke_test.sh --auto
```

## License

MIT
