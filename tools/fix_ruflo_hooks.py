import os
import json

script_content = """const fs = require('fs');
const { spawn } = require('child_process');

// Read all stdin
let inputBuffer = '';
process.stdin.setEncoding('utf8');

process.stdin.on('data', (chunk) => {
    inputBuffer += chunk;
});

process.stdin.on('end', () => {
    let inputJson = {};
    try {
        if (inputBuffer.trim()) {
            inputJson = JSON.parse(inputBuffer);
        }
    } catch (e) {
        // ignore parse error
    }

    const action = process.argv[2] || '';
    
    // Check if ruflo command exists on Windows or Unix
    const rufloCmd = process.platform === 'win32' ? 'ruflo.cmd' : 'ruflo';
    
    // We execute ruflo safely or silently complete if not installed
    try {
        const child = spawn(rufloCmd, ['hook', action], {
            stdio: ['pipe', 'ignore', 'ignore'],
            windowsHide: true,
            shell: true
        });
        
        child.on('error', () => {
            process.exit(0);
        });

        if (inputBuffer) {
            child.stdin.write(inputBuffer);
        }
        child.stdin.end();

        child.on('close', () => {
            process.exit(0);
        });
    } catch (err) {
        process.exit(0);
    }
});

// Set a timeout safeguard so the hook never hangs Claude Code
setTimeout(() => {
    process.exit(0);
}, 3000);
"""

paths_to_fix = [
    r"C:\Users\yemin\.claude\plugins\cache\ruflo\ruflo-core\0.2.2",
    r"C:\Users\yemin\.claude\plugins\marketplaces\ruflo\plugins\ruflo-core",
    r"C:\Users\yemin\.claude\plugins\marketplaces\ruflo\.claude-plugin"
]

for base_dir in paths_to_fix:
    if not os.path.exists(base_dir):
        continue
    
    scripts_dir = os.path.join(base_dir, 'scripts')
    os.makedirs(scripts_dir, exist_ok=True)
    hook_script = os.path.join(scripts_dir, 'ruflo-hook.cjs')
    with open(hook_script, 'w', encoding='utf-8') as f:
        f.write(script_content)
    print(f"Wrote hook script: {hook_script}")
    
    hooks_json = os.path.join(base_dir, 'hooks', 'hooks.json')
    if os.path.exists(hooks_json):
        try:
            with open(hooks_json, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            modified = False
            for event, matchers in data.get('hooks', {}).items():
                for matcher in matchers:
                    for hook in matcher.get('hooks', []):
                        cmd = hook.get('command', '')
                        if '/bin/bash' in cmd or 'ruflo hook' in cmd or 'INPUT=' in cmd:
                            action_arg = event
                            hook['command'] = f'node "${{CLAUDE_PLUGIN_ROOT}}/scripts/ruflo-hook.cjs" {action_arg}'
                            modified = True
            
            if modified:
                with open(hooks_json, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2)
                print(f"Updated hooks.json: {hooks_json}")
        except Exception as e:
            print(f"Error processing {hooks_json}: {e}")
