#!/bin/bash
# usage: stop_console.sh <port>
pid=$(netstat -ano | grep ":$1 " | grep LISTENING | awk '{print $5}' | head -1); [ -n "$pid" ] && taskkill //F //PID $pid > /dev/null 2>&1; echo stopped $1
