//go:build windows

package main

import (
	"golang.org/x/sys/windows/svc"
)

// NOT RUN ON REAL WINDOWS YET: compiles under GOOS=windows only. Registered with
// deploy/windows/install-service.ps1; the Service Control Manager's Stop request closes
// the stop channel, which makes serve() stop llama-server before the service exits.
func isService() bool {
	ok, err := svc.IsWindowsService()
	return err == nil && ok
}

type handler struct{ run func(stop <-chan struct{}) }

func (h handler) Execute(_ []string, req <-chan svc.ChangeRequest, status chan<- svc.Status) (bool, uint32) {
	const accepts = svc.AcceptStop | svc.AcceptShutdown
	status <- svc.Status{State: svc.StartPending}
	stop := make(chan struct{})
	done := make(chan struct{})
	go func() { h.run(stop); close(done) }()
	status <- svc.Status{State: svc.Running, Accepts: accepts}
	for {
		select {
		case c := <-req:
			switch c.Cmd {
			case svc.Interrogate:
				status <- c.CurrentStatus
			case svc.Stop, svc.Shutdown:
				status <- svc.Status{State: svc.StopPending}
				close(stop)
				<-done
				return false, 0
			}
		case <-done:
			return false, 1
		}
	}
}

func runService(run func(stop <-chan struct{})) {
	svc.Run("AICFWorkerGuard", handler{run})
}
