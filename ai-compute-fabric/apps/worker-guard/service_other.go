//go:build !windows

package main

func isService() bool { return false }

func runService(run func(stop <-chan struct{})) { run(nil) }
