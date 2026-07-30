#!/bin/bash
if (($#!=1))
then
  echo "usage: $0 {br_name}"
  exit 1
fi

brName=$1

ifconfig ${brName} >/dev/null 2>&1

if (($?==0))
then
    echo "1" # 存在
else
    echo "0" # 不存在
fi
