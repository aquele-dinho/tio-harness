// Comando tioh-agent: camada de orquestração (Layer 2) do Tio Harness, em Go.
package main

import (
	"os"

	"github.com/aquele-dinho/tio-harness/harness/internal/cli"
)

func main() {
	os.Exit(cli.Execute(os.Stdout, os.Stderr, os.Args[1:]))
}
