from typing import Dict, Generator, Tuple, Union
import argparse
import contextlib
import concurrent.futures
import json
import os
import subprocess
import sys
import tempfile
import threading

updates: Dict[concurrent.futures.Future, Dict] = {}

TempDirs = Dict[str, Tuple[str, str, threading.Lock]]

thread_name_prefix = 'UpdateScriptThread'

def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)

def run_update_script(package: Dict, commit: bool, temp_dirs: TempDirs) -> subprocess.CompletedProcess:
    worktree: Union[None, str] = None

    if commit and 'commit' in package['supportedFeatures']:
        thread_name = threading.current_thread().name
        worktree, _branch, lock = temp_dirs[thread_name]
        lock.acquire()
        package['thread'] = thread_name

    eprint(f" - {package['name']}: UPDATING ...")

    return subprocess.run(package['updateScript'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, cwd=worktree)

@contextlib.contextmanager
def make_worktree() -> Generator[Tuple[str, str], None, None]:
    with tempfile.TemporaryDirectory() as wt:
        branch_name = f'update-{os.path.basename(wt)}'
        target_directory = f'{wt}/nixpkgs'

        subprocess.run(['git', 'worktree', 'add', '-b', branch_name, target_directory], check=True)
        yield (target_directory, branch_name)
        subprocess.run(['git', 'worktree', 'remove', target_directory], check=True)
        subprocess.run(['git', 'branch', '-D', branch_name], check=True)

def commit_changes(worktree: str, branch: str, execution: subprocess.CompletedProcess) -> None:
    changes = json.loads(execution.stdout)
    for change in changes:
        subprocess.run(['git', 'add'] + change['files'], check=True, cwd=worktree)
        commit_message = '{attrPath}: {oldVersion} → {newVersion}'.format(**change)
        subprocess.run(['git', 'commit', '-m', commit_message], check=True, cwd=worktree)
        subprocess.run(['git', 'cherry-pick', branch], check=True)

def merge_changes(package: Dict, future: concurrent.futures.Future, commit: bool, keep_going: bool, temp_dirs: TempDirs) -> None:
    try:
        execution = future.result()
        if commit and 'commit' in package['supportedFeatures']:
            thread_name = package['thread']
            worktree, branch, lock = temp_dirs[thread_name]
            commit_changes(worktree, branch, execution)
        eprint(f" - {package['name']}: DONE.")
    except subprocess.CalledProcessError as e:
        eprint(f" - {package['name']}: ERROR")
        eprint()
        eprint(f"--- SHOWING ERROR LOG FOR {package['name']} ----------------------")
        eprint()
        eprint(e.stdout.decode('utf-8'))
        with open(f"{package['pname']}.log", 'wb') as logfile:
            logfile.write(e.stdout)
        eprint()
        eprint(f"--- SHOWING ERROR LOG FOR {package['name']} ----------------------")

        if not keep_going:
            sys.exit(1)
    finally:
        if commit and 'commit' in package['supportedFeatures']:
            lock.release()

def main(max_workers: int, keep_going: bool, commit: bool, packages: Dict) -> None:
    with open(sys.argv[1]) as f:
        packages = json.load(f)

    eprint()
    eprint('Going to be running update for following packages:')
    for package in packages:
        eprint(f" - {package['name']}")
    eprint()

    confirm = input('Press Enter key to continue...')
    if confirm == '':
        eprint()
        eprint('Running update for:')

        with contextlib.ExitStack() as stack, concurrent.futures.ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=thread_name_prefix) as executor:
            if commit:
                temp_dirs = {f'{thread_name_prefix}_{str(i)}': (*stack.enter_context(make_worktree()), threading.Lock()) for i in range(max_workers)}
            else:
                temp_dirs = {}

            for package in packages:
                updates[executor.submit(run_update_script, package, commit, temp_dirs)] = package

            for future in concurrent.futures.as_completed(updates):
                package = updates[future]

                merge_changes(package, future, commit, keep_going, temp_dirs)

        eprint()
        eprint('Packages updated!')
        sys.exit()
    else:
        eprint('Aborting!')
        sys.exit(130)

parser = argparse.ArgumentParser(description='Update packages')
parser.add_argument('--max-workers', '-j', dest='max_workers', type=int, help='Number of updates to run concurrently', nargs='?', default=4)
parser.add_argument('--keep-going', '-k', dest='keep_going', action='store_true', help='Do not stop after first failure')
parser.add_argument('--commit', '-c', dest='commit', action='store_true', help='Commit the changes')
parser.add_argument('packages', help='JSON file containing the list of package names and their update scripts')

if __name__ == '__main__':
    args = parser.parse_args()

    try:
        main(args.max_workers, args.keep_going, args.commit, args.packages)
    except (KeyboardInterrupt, SystemExit) as e:
        for update in updates:
            update.cancel()

        sys.exit(e.code if isinstance(e, SystemExit) else 130)
