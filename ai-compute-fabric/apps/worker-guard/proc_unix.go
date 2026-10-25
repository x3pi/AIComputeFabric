//go:build !windows

package main

import (
	"os/exec"
	"syscall"
)

// The engine runs in its own process group so one signal reaches llama-server and
// anything it spawned.
func prepareCmd(cmd *exec.Cmd) { cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true} }

// softStop asks the process group to exit; ok=false means the platform has no graceful stop.
func softStop(cmd *exec.Cmd) (name string, ok bool) {
	syscall.Kill(-cmd.Process.Pid, syscall.SIGTERM)
	return "SIGTERM", true
}

func hardStop(cmd *exec.Cmd) string {
	syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
	return "SIGKILL"
}
