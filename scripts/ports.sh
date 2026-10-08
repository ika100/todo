#!/usr/bin/env bash
# Host-port helpers for local-cluster.sh (sourced, and unit-tested in the platform repo).

# port_in_use <port>: success when something listens on the TCP port of this machine
port_in_use() {
  if command -v lsof > /dev/null 2>&1; then
    lsof -nP -iTCP:"$1" -sTCP:LISTEN > /dev/null 2>&1
  else
    (exec 3<> "/dev/tcp/127.0.0.1/$1") 2> /dev/null
  fi
}

# port_owner <port>: "command (pid N)" of the listener, or nothing when unknown
port_owner() {
  command -v lsof > /dev/null 2>&1 || return 0
  lsof -nP -iTCP:"$1" -sTCP:LISTEN 2> /dev/null | awk 'NR==2 {print $1 " (pid " $2 ")"}'
}

# free_port <first>: the first free port from <first> up to <first>+50, nothing when all are taken
free_port() {
  local p
  for p in $(seq "$1" $(($1 + 50))); do
    if ! port_in_use "$p"; then
      echo "$p"
      return 0
    fi
  done
  return 1
}
