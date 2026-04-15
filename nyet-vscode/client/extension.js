'use strict';

const path = require('path');
const fs = require('fs');
const vscode = require('vscode');

let client;

function findServerScript(extensionPath) {
  const config = vscode.workspace.getConfiguration('nyet');
  const configured = config.get('lspServerPath', '');
  if (configured && fs.existsSync(configured)) return configured;

  // Walk up from the extension dir looking for lsp/server.py
  let dir = extensionPath;
  for (let i = 0; i < 4; i++) {
    const candidate = path.join(dir, 'lsp', 'server.py');
    if (fs.existsSync(candidate)) return candidate;
    dir = path.dirname(dir);
  }
  return null;
}

function activate(context) {
  const config = vscode.workspace.getConfiguration('nyet');

  if (!config.get('lspEnabled', true)) return;

  let LanguageClient, TransportKind;
  try {
    ({ LanguageClient, TransportKind } = require('vscode-languageclient/node'));
  } catch (_) {
    vscode.window.showWarningMessage(
      'Nyet: LSP features are disabled. Run `npm install` inside the nyet-vscode/ directory to enable them.'
    );
    return;
  }

  const serverScript = findServerScript(context.extensionPath);
  if (!serverScript) {
    vscode.window.showWarningMessage(
      'Nyet: Could not locate lsp/server.py. Set "nyet.lspServerPath" in settings.'
    );
    return;
  }

  const pythonPath = config.get('pythonPath', 'python3');

  const serverOptions = {
    run: {
      command: pythonPath,
      args: [serverScript],
      transport: TransportKind.stdio,
    },
    debug: {
      command: pythonPath,
      args: [serverScript, '--debug'],
      transport: TransportKind.stdio,
    },
  };

  const clientOptions = {
    documentSelector: [{ scheme: 'file', language: 'nyet' }],
    synchronize: {
      fileEvents: vscode.workspace.createFileSystemWatcher('**/*.no'),
    },
    outputChannelName: 'Nyet Language Server',
  };

  client = new LanguageClient(
    'nyetLanguageServer',
    'Nyet Language Server',
    serverOptions,
    clientOptions
  );

  client.start();

  context.subscriptions.push({
    dispose: () => { if (client) client.stop(); },
  });
}

function deactivate() {
  if (!client) return undefined;
  return client.stop();
}

module.exports = { activate, deactivate };
