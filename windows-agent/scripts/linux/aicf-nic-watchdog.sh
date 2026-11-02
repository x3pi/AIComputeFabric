#!/bin/sh
# Reset enp133s0 neu RX dung trong khi TX tang (r8169 RX hang). Chay moi phut qua systemd timer.
IF=enp133s0; ST=/run/aicf-nic-watchdog.state
rx=$(cat /sys/class/net/$IF/statistics/rx_packets); tx=$(cat /sys/class/net/$IF/statistics/tx_packets)
[ "$(cat /sys/class/net/$IF/carrier 2>/dev/null)" = 1 ] || { rm -f $ST; exit 0; }
if [ -f $ST ]; then
  read orx otx < $ST
  if [ "$rx" = "$orx" ] && [ $((tx-otx)) -ge 5 ]; then
    logger -t aicf-nic-watchdog "RX frozen ($rx) while TX +$((tx-otx)): flapping $IF"
    ip link set $IF down; sleep 2; ip link set $IF up; rm -f $ST; exit 0
  fi
fi
echo "$rx $tx" > $ST
