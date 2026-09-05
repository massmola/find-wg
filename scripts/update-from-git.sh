#!/usr/bin/env bash
set -Eeuo pipefail

main() {
    local project_dir upstream_ref remote_name local_commit upstream_commit
    local target_commit container_id deployed_commit container_running
    local previous_image_id new_container_id new_running new_commit new_restarts
    local -a required_tracked_files

    project_dir="${FIND_APARTMENT_DIR:-/opt/find-apartment}"
    cd "$project_dir"

    if [[ ! -d .git ]]; then
        echo "ERROR: $project_dir is not a Git clone" >&2
        exit 1
    fi

    required_tracked_files=(
        Dockerfile
        compose.yaml
        notification_bot.py
        test_notification_bot.py
        scripts/update-from-git.sh
    )
    if ! git ls-files --error-unmatch -- "${required_tracked_files[@]}" >/dev/null 2>&1; then
        echo "ERROR: essential deployment files are not tracked by Git" >&2
        exit 1
    fi

    # Serialize invocations of this updater (including manual invocations).
    exec 9>".git/find-apartment-update.lock"
    if ! flock --nonblock 9; then
        echo "Another apartment-bot update is already running; skipping."
        exit 0
    fi

    if ! git diff --quiet || ! git diff --cached --quiet; then
        echo "ERROR: tracked files have local changes; refusing to overwrite them" >&2
        exit 1
    fi

    upstream_ref="$(git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}')" || {
        echo "ERROR: the checked-out branch has no configured upstream" >&2
        exit 1
    }
    remote_name="${upstream_ref%%/*}"

    export GIT_TERMINAL_PROMPT=0
    export GIT_SSH_COMMAND="ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=15"
    git fetch --prune "$remote_name"

    local_commit="$(git rev-parse HEAD)"
    upstream_commit="$(git rev-parse '@{upstream}')"
    if [[ "$local_commit" != "$upstream_commit" ]]; then
        if ! git merge-base --is-ancestor "$local_commit" "$upstream_commit"; then
            echo "ERROR: local and upstream branches diverged; manual intervention required" >&2
            exit 1
        fi
        git merge --ff-only "$upstream_ref"

        # The update may have changed this script. Re-execute the checked-out
        # version so the deployment always uses the code from the new commit.
        flock --unlock 9
        exec bash "$project_dir/scripts/update-from-git.sh"
    fi

    target_commit="$(git rev-parse HEAD)"
    container_id="$(docker compose ps --all --quiet apartment-bot)"
    deployed_commit=""
    container_running="false"
    previous_image_id=""
    if [[ -n "$container_id" ]]; then
        deployed_commit="$(
            docker inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' \
                "$container_id" 2>/dev/null || true
        )"
        container_running="$(
            docker inspect --format '{{ .State.Running }}' "$container_id" 2>/dev/null || true
        )"
        previous_image_id="$(
            docker inspect --format '{{ .Image }}' "$container_id" 2>/dev/null || true
        )"
    fi

    if [[ "$deployed_commit" == "$target_commit" && "$container_running" == "true" ]]; then
        echo "Apartment bot is already running Git revision $target_commit."
        exit 0
    fi

    export GIT_COMMIT="$target_commit"
    docker compose config --quiet
    docker compose build --pull apartment-bot
    docker compose run --rm --no-deps --entrypoint python apartment-bot -m unittest -q
    docker compose run --rm --no-deps apartment-bot --help >/dev/null
    rollback() {
        if [[ -n "$previous_image_id" ]]; then
            echo "Rolling back to the previously running image $previous_image_id." >&2
            docker image tag "$previous_image_id" find-apartment-bot:local
            export GIT_COMMIT="$deployed_commit"
            docker compose up --detach --no-build --pull never --force-recreate apartment-bot
        fi
    }
    if ! docker compose up --detach --no-build --pull never apartment-bot; then
        rollback
        exit 1
    fi

    # Catch immediate startup failures before considering the revision deployed.
    sleep 10

    new_container_id="$(docker compose ps --all --quiet apartment-bot)"
    new_running="false"
    if [[ -n "$new_container_id" ]]; then
        new_running="$(
            docker inspect --format '{{ .State.Running }}' "$new_container_id" 2>/dev/null || true
        )"
    fi
    new_commit="$(
        docker inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' \
            "$new_container_id" 2>/dev/null || true
    )"
    new_restarts="$(docker inspect --format '{{ .RestartCount }}' "$new_container_id" 2>/dev/null || true)"
    if [[ "$new_commit" != "$target_commit" || "$new_running" != "true" || "$new_restarts" != "0" ]]; then
        echo "ERROR: revision $target_commit failed its post-deployment check" >&2
        rollback
        exit 1
    fi

    echo "Apartment bot deployed successfully at Git revision $target_commit."
}

main "$@"
