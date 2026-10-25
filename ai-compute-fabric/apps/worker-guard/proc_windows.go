//go:build windows

package main

import (
	"os/exec"
	"strconv"
	"syscall"
)

const createNoWindow = 0x08000000

// NOT RUN ON REAL WINDOWS YET: this file compiles (GOOS=windows) but its behaviour is
// unverified. Windows has no SIGTERM for a console-less child, and llama-server keeps
// no state worth saving (inference is stateless), so stop is immediate termination of
// the whole process tree.
func prepareCmd(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: syscall.CREATE_NEW_PROCESS_GROUP | createNoWindow, HideWindow: true}
}

func softStop(cmd *exec.Cmd) (string, bool) { return "", false }

func hardStop(cmd *exec.Cmd) string {
	if err := exec.Command("taskkill", "/PID", strconv.Itoa(cmd.Process.Pid), "/T", "/F").Run(); err != nil {
		cmd.Process.Kill()
	}
	return "TerminateProcess"
}
