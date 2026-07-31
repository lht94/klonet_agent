# Ops 半永久环境基线

- updated_at: 2026-07-03T15:56:31+08:00

inspect_ops_context
## baseline
- os_release: detected - NAME="Ubuntu" | VERSION="20.04.6 LTS (Focal Fossa)" | ID=ubuntu | ID_LIKE=debian | PRETTY_NAME="Ubuntu 20.04.6 LTS" | VERSION_ID="20.04" | HOME_URL="https://www.ubuntu.com/" | SUPPORT_URL="https://help.ubuntu.com/" | BUG_REPORT_URL="https://bugs.launchpad.net/ubuntu/" | PRIVACY_POLICY_URL="https://www.ubuntu.com/legal/terms-and-policies/privacy-policy" | VERSION_CODENAME=focal | UBUNTU_CODENAME=focal
- kernel: detected - 5.4.0-216-generic
- arch: detected - x86_64
- cpu: detected - Architecture:                       x86_64 | CPU(s):                             16 | On-line CPU(s) list:                0-15 | Model name:                         Intel Xeon Processor (Cascadelake) | Virtualization:                     VT-x | Virtualization type:                full | NUMA node0 CPU(s):                  0-15
- memory: detected - total        used        free      shared  buff/cache   available | Mem:           15Gi       1.1Gi       9.1Gi       1.0Mi       5.4Gi        14Gi | Swap:         2.9Gi          0B       2.9Gi
- disk: detected - total=125.9GB free=103.3GB
- virtualization: detected - 16
- python: detected - /home/lzl/miniconda3/envs/klonet_agent/bin/python3 | Python 3.11.15 | sh: 1: /usr/local/python3/bin/python3.8: not found
- rust: unchecked - exit 127
- docker_version: missing - no output
- compose_version: missing - no output
- ovs: unchecked - inactive
- kvm: detected - kvm_intel             286720  0 | kvm                   667648  1 kvm_intel
- libvirt: unchecked - inactive
## runtime
- ports: detected - State     Recv-Q    Send-Q       Local Address:Port        Peer Address:Port    Process | LISTEN    0         4096             127.0.0.1:37553            0.0.0.0:*        users:(("code-07ff9d6178",pid=7018,fd=9)) | LISTEN    0         4096         127.0.0.53%lo:53               0.0.0.0:* | LISTEN    0         128                0.0.0.0:22               0.0.0.0:* | LISTEN    0         128              127.0.0.1:38889            0.0.0.0:*        users:(("code-7e7950df89",pid=3467,fd=12)) | LISTEN    0         128                   [::]:22                  [::]:*
- services: detected - accounts-daemon.service     loaded active running Accounts Service | atd.service                 loaded active running Deferred execution scheduler | cron.service                loaded active running Regular background program processing daemon | dbus.service                loaded active running D-Bus System Message Bus | fwupd.service               loaded active running Firmware update daemon | getty@tty1.service          loaded active running Getty on tty1 | irqbalance.service          loaded active running irqbalance daemon | ModemManager.service        loaded active running Modem Manage...
- screen: unchecked - No Sockets found in /run/screen/S-lzl.
- processes: detected - pid=16552 cwd=/home/lzl/klonet_agent cmd=python3 -m klonet_agent.agent --mode ops --user-id lht --project-id test | pid=16745 cwd=? cmd=
- docker_containers: unchecked - docker not found
- docker_images: missing - no output
- docker_networks: unchecked - docker not found
- redis: unchecked - inactive
- mysql: unchecked - inactive
- rabbitmq: unchecked - inactive
- nginx: unchecked - inactive
