#!/bin/bash -e

cd /

# Configuration variables
REPO_CACHE_DIR=/tmp/gistsrepo
GISTS_TAG='gists'
LOCK=/tmp/gistsupdater.lock
LOCK_EXPIRE_S=600
LOG=/tmp/gistsupdater.log
GIT_REPO=https://github.com/dberzano/raspbian-ponyo
REPO_BRANCH=master

# Refuse to run as root
if [[ $(whoami) == root ]]; then
    echo 'refusing to run as root'
    exit 1
fi

# Lock logic: store pidfile in lock symlink
if ! ln -ns $$ $LOCK &> /dev/null; then
    # Get pid value, timestamp and elapsed seconds since lock creation
    LOCK_PID=$(readlink $LOCK)
    LOCK_TS=$(stat -c %Y $LOCK)
    NOW_S=$(date +%s)
    DELTA_S=$((NOW_S - LOCK_TS))

    # Is pid active?
    if kill -0 $LOCK_PID &> /dev/null; then
        # Has it been active for too long?
        if [[ $DELTA_S -gt $LOCK_EXPIRE_S ]]; then
            echo "found active process ($LOCK_PID) running for too long (over $DELTA_S s): killing it and acquiring lock"
            kill -9 $LOCK_PID || true
            ln -ns $$ $LOCK
        fi
        echo "found active process ($LOCK_PID): leaving it alone, exiting"
        exit 0
    else
        # Process not found
        echo "no active process was found, but stale pidfile: acquiring lock"
        ln -nfs $$ $LOCK  # note the -f for force
    fi
fi
echo "lock has been acquired: $(readlink $LOCK)"

# Clone repository and refresh
if [[ ! -d $REPO_CACHE_DIR/.git ]]; then
    echo 'gists repo clone not found: cloning it'
    mkdir -p "$REPO_CACHE_DIR"
    git clone "$GIT_REPO" "$REPO_CACHE_DIR"
fi

# Refresh existing repository
echo 'getting updates from remote git repository'
git -C "$REPO_CACHE_DIR" fetch --all
git -C "$REPO_CACHE_DIR" checkout "$REPO_BRANCH"
git -C "$REPO_CACHE_DIR" reset --hard origin/"$REPO_BRANCH"

# Run Ansible locally
echo "running Ansible on tag \`$GISTS_TAG\` only as user $(whoami)"
pushd "$REPO_CACHE_DIR"
sed -e 's/^\s*vault_password_file.*$//g' ansible.cfg > ansible.cfg.0
mv ansible.cfg.0 ansible.cfg
git diff
ansible-playbook site.yml --tags "$GISTS_TAG" -c local -i localhost, --vault-password-file "$HOME/.ansible_vault_password" 2>&1 | tee $LOG
popd

# In case everything goes ok, we remove the lockfile
rm -f $LOCK
echo 'lock removed'