---
name: 'tioh-bootstrap'
description: 'Gera o briefing de um projeto a partir de um documento descritivo e instala o Tio Harness nele'
agent: 'agent'
argument-hint: '<documento ou pageId> <repositório alvo>'
---
Siga a skill [framework-bootstrap](../../.agents/skills/framework-bootstrap/SKILL.md) e o prompt canônico
[bootstrap/briefing-from-document.md](../../bootstrap/briefing-from-document.md).

Origem: ${input:origem:Documento, caminho ou pageId do Confluence que descreve o projeto}
Destino: ${input:destino:Caminho do repositório onde instalar o harness}
