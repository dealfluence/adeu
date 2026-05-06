const { spawn, execSync } = require('child_process');
const os = require('os');
const path = require('path');

const homedir = os.homedir();
const isWin = process.platform === 'win32';
const isMac = process.platform === 'darwin';

// 1. Pre-emptively heal PATH (GUI apps often miss user bin directories)
const customPaths = isWin
    ? [
        path.join(homedir, 'AppData', 'Roaming', 'uv', 'bin'),
        path.join(homedir, 'AppData', 'Local', 'Programs', 'uv', 'bin'),
        path.join(homedir, '.cargo', 'bin'),
        // Add typical Python Scripts paths where pip installs uv
        path.join(homedir, 'AppData', 'Local', 'Programs', 'Python', 'Python312', 'Scripts'),
        path.join(homedir, 'AppData', 'Local', 'Programs', 'Python', 'Python311', 'Scripts')
    ]
    : [
        path.join(homedir, '.local', 'bin'),
        path.join(homedir, '.cargo', 'bin'),
        '/usr/local/bin',
        '/opt/homebrew/bin'
    ];

process.env.PATH = `${process.env.PATH}${path.delimiter}${customPaths.join(path.delimiter)}`;

// 2. Safe check for 'uv'
let uvFound = false;
try {
    execSync(isWin ? 'uvx.exe --version' : 'uvx --version', { stdio: 'ignore' });
    uvFound = true;
} catch (e) {
    // uv not found
}

if (!uvFound) {
    // Graceful, transparent failure using standard package managers
    console.error("=====================================================");
    console.error("❌ ADEU SERVER FAILED TO START: 'uv' NOT FOUND");
    console.error("=====================================================");
    console.error("Adeu requires the 'uv' Python package manager to run locally.");
    console.error("Please install it using your preferred package manager:\n");
    
    if (isMac) {
        console.error("  Using Homebrew (Recommended):");
        console.error("    brew install uv\n");
    }
    
    console.error("  Using pip (Requires Python):");
    console.error("    pip install uv\n");

    console.error("After installing, restart Claude Desktop.");
    console.error("=====================================================");
    process.exit(1);
}

// 3. Launch the server safely via uvx
const uvxCmd = isWin ? 'uvx.exe' : 'uvx';
const server = spawn(uvxCmd, ['--quiet', '--python', '3.12', '--from', 'adeu', 'adeu-server'], {
    env: process.env,
    stdio: ['pipe', 'pipe', 'inherit'] 
});

server.on('error', (err) => {
    console.error(`[Adeu Bootstrapper] Process spawn failed: ${err.message}`);
    process.exit(1);
});

// 4. Bridge the MCP JSON-RPC
process.stdin.pipe(server.stdin);
server.stdout.pipe(process.stdout);