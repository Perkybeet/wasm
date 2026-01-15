#!/usr/bin/env bash
# Bash completion script for WASM (Web App System Management)
#
# Installation:
#   Option 1 - System-wide (requires sudo):
#     sudo cp wasm.bash /etc/bash_completion.d/wasm
#
#   Option 2 - User-local:
#     mkdir -p ~/.local/share/bash-completion/completions
#     cp wasm.bash ~/.local/share/bash-completion/completions/wasm
#
#   Option 3 - Manual in .bashrc:
#     source /path/to/wasm.bash
#
#   After installation, restart your shell or run: source ~/.bashrc

# Helper function to get list of deployed apps (domains)
_wasm_get_apps() {
    local apps_dir="/var/www/apps"
    if [[ -d "$apps_dir" ]]; then
        for app in "$apps_dir"/*/; do
            if [[ -d "$app" ]]; then
                basename "$app"
            fi
        done 2>/dev/null
    fi
}

# Helper function to get list of wasm services
_wasm_get_services() {
    systemctl list-units --type=service --all 2>/dev/null | \
        grep -E '^wasm-[^ ]+\.service' | \
        sed 's/^\(wasm-[^ ]*\)\.service.*/\1/' | \
        sed 's/^wasm-//'
}

# Helper function to get nginx sites
_wasm_get_nginx_sites() {
    local sites_dir="/etc/nginx/sites-available"
    if [[ -d "$sites_dir" ]]; then
        for site in "$sites_dir"/*; do
            if [[ -f "$site" && "$(basename "$site")" != "default" ]]; then
                basename "$site"
            fi
        done 2>/dev/null
    fi
}

# Helper function to get apache sites
_wasm_get_apache_sites() {
    local sites_dir="/etc/apache2/sites-available"
    if [[ -d "$sites_dir" ]]; then
        for site in "$sites_dir"/*.conf; do
            if [[ -f "$site" ]]; then
                basename "$site" .conf
            fi
        done 2>/dev/null
    fi
}

# Helper function to get all sites
_wasm_get_all_sites() {
    _wasm_get_nginx_sites
    _wasm_get_apache_sites
}

# Helper function to get SSL certificates
_wasm_get_certs() {
    local live_dir="/etc/letsencrypt/live"
    if [[ -d "$live_dir" ]]; then
        for cert in "$live_dir"/*/; do
            if [[ -d "$cert" ]]; then
                basename "$cert"
            fi
        done 2>/dev/null
    fi
}

# Helper function to get backup IDs
_wasm_get_backups() {
    local backup_dir="/var/www/backups"
    if [[ -d "$backup_dir" ]]; then
        for backup in "$backup_dir"/*.tar.gz; do
            if [[ -f "$backup" ]]; then
                basename "$backup" .tar.gz
            fi
        done 2>/dev/null
    fi
}

# Main completion function
_wasm_completion() {
    local cur prev words cword
    _init_completion || return

    # Top-level commands
    local commands="create new deploy list ls status info restart stop start update upgrade delete remove rm logs health site service svc cert ssl certificate setup backup bak rollback rb db database web store monitor mon --help --version --verbose --interactive --no-color --dry-run --json --changelog"

    # Webapp actions that take a domain
    local webapp_domain_commands="status info restart stop start update upgrade delete remove rm logs"

    # Site actions
    local site_actions="create list ls enable disable delete remove rm show cat"

    # Service actions
    local service_actions="create list ls status info start stop restart logs delete remove rm"

    # Cert actions
    local cert_actions="create obtain new list ls info show renew revoke delete remove rm"

    # Setup actions
    local setup_actions="init completions permissions ssh doctor"

    # Backup actions
    local backup_actions="create new list ls restore delete remove rm verify check info show storage"

    # DB actions
    local db_actions="install uninstall status start stop restart engines create drop list ls info user-create user-delete user-list grant revoke backup restore backups query connect connection-string config"

    # Web actions
    local web_actions="start stop status restart token install"

    # Store actions
    local store_actions="init stats import export sync path"

    # Monitor actions
    local monitor_actions="status scan run enable install disable uninstall test-email config"

    # DB engines
    local db_engines="mysql postgresql redis mongodb"

    # App types
    local app_types="nextjs nodejs vite python static auto"

    # Webservers
    local webservers="nginx apache"

    # Package managers
    local package_managers="npm pnpm bun auto"

    case ${cword} in
        1)
            # First argument - complete commands
            COMPREPLY=($(compgen -W "${commands}" -- "${cur}"))
            return
            ;;
    esac

    # Get the command (first non-option argument)
    local cmd=""
    local subcmd=""
    local i
    for ((i=1; i < cword; i++)); do
        case "${words[i]}" in
            -*)
                continue
                ;;
            *)
                if [[ -z "$cmd" ]]; then
                    cmd="${words[i]}"
                elif [[ -z "$subcmd" ]]; then
                    subcmd="${words[i]}"
                fi
                ;;
        esac
    done

    # Handle site subcommands
    if [[ "$cmd" == "site" ]]; then
        case "$subcmd" in
            "")
                COMPREPLY=($(compgen -W "${site_actions}" -- "${cur}"))
                return
                ;;
            create)
                case "$prev" in
                    -d|--domain)
                        return
                        ;;
                    -w|--webserver)
                        COMPREPLY=($(compgen -W "${webservers}" -- "${cur}"))
                        return
                        ;;
                    -t|--template)
                        COMPREPLY=($(compgen -W "proxy static" -- "${cur}"))
                        return
                        ;;
                    -p|--port)
                        return
                        ;;
                esac
                COMPREPLY=($(compgen -W "-d --domain -w --webserver -t --template -p --port" -- "${cur}"))
                return
                ;;
            enable|disable|show|cat)
                if [[ "$prev" == "$subcmd" ]]; then
                    COMPREPLY=($(compgen -W "$(_wasm_get_all_sites)" -- "${cur}"))
                fi
                return
                ;;
            delete|remove|rm)
                case "$prev" in
                    delete|remove|rm)
                        COMPREPLY=($(compgen -W "$(_wasm_get_all_sites)" -- "${cur}"))
                        return
                        ;;
                esac
                COMPREPLY=($(compgen -W "-f --force -y" -- "${cur}"))
                return
                ;;
            list|ls)
                case "$prev" in
                    -w|--webserver)
                        COMPREPLY=($(compgen -W "nginx apache all" -- "${cur}"))
                        return
                        ;;
                esac
                COMPREPLY=($(compgen -W "-w --webserver" -- "${cur}"))
                return
                ;;
        esac
        return
    fi

    # Handle service subcommands
    if [[ "$cmd" == "service" || "$cmd" == "svc" ]]; then
        case "$subcmd" in
            "")
                COMPREPLY=($(compgen -W "${service_actions}" -- "${cur}"))
                return
                ;;
            create)
                case "$prev" in
                    -n|--name)
                        return
                        ;;
                    -c|--command)
                        return
                        ;;
                    -d|--directory)
                        _filedir -d
                        return
                        ;;
                    -u|--user)
                        COMPREPLY=($(compgen -u -- "${cur}"))
                        return
                        ;;
                    --description)
                        return
                        ;;
                esac
                COMPREPLY=($(compgen -W "-n --name -c --command -d --directory -u --user --description" -- "${cur}"))
                return
                ;;
            status|info|start|stop|restart)
                if [[ "$prev" == "$subcmd" ]]; then
                    COMPREPLY=($(compgen -W "$(_wasm_get_services)" -- "${cur}"))
                fi
                return
                ;;
            logs)
                case "$prev" in
                    logs)
                        COMPREPLY=($(compgen -W "$(_wasm_get_services)" -- "${cur}"))
                        return
                        ;;
                    -n|--lines)
                        return
                        ;;
                esac
                COMPREPLY=($(compgen -W "-f --follow -n --lines" -- "${cur}"))
                return
                ;;
            delete|remove|rm)
                case "$prev" in
                    delete|remove|rm)
                        COMPREPLY=($(compgen -W "$(_wasm_get_services)" -- "${cur}"))
                        return
                        ;;
                esac
                COMPREPLY=($(compgen -W "-f --force -y" -- "${cur}"))
                return
                ;;
            list|ls)
                COMPREPLY=($(compgen -W "-a --all" -- "${cur}"))
                return
                ;;
        esac
        return
    fi

    # Handle cert subcommands
    if [[ "$cmd" == "cert" || "$cmd" == "ssl" || "$cmd" == "certificate" ]]; then
        case "$subcmd" in
            "")
                COMPREPLY=($(compgen -W "${cert_actions}" -- "${cur}"))
                return
                ;;
            create|obtain|new)
                case "$prev" in
                    -d|--domain)
                        return
                        ;;
                    -e|--email)
                        return
                        ;;
                    -w|--webroot)
                        _filedir -d
                        return
                        ;;
                esac
                COMPREPLY=($(compgen -W "-d --domain -e --email -w --webroot --standalone --nginx --apache --dry-run" -- "${cur}"))
                return
                ;;
            info|show)
                if [[ "$prev" == "$subcmd" ]]; then
                    COMPREPLY=($(compgen -W "$(_wasm_get_certs)" -- "${cur}"))
                fi
                return
                ;;
            renew)
                case "$prev" in
                    -d|--domain)
                        COMPREPLY=($(compgen -W "$(_wasm_get_certs)" -- "${cur}"))
                        return
                        ;;
                esac
                COMPREPLY=($(compgen -W "-d --domain --force --dry-run" -- "${cur}"))
                return
                ;;
            revoke)
                if [[ "$prev" == "revoke" ]]; then
                    COMPREPLY=($(compgen -W "$(_wasm_get_certs)" -- "${cur}"))
                    return
                fi
                COMPREPLY=($(compgen -W "--delete" -- "${cur}"))
                return
                ;;
            delete|remove|rm)
                case "$prev" in
                    delete|remove|rm)
                        COMPREPLY=($(compgen -W "$(_wasm_get_certs)" -- "${cur}"))
                        return
                        ;;
                esac
                COMPREPLY=($(compgen -W "-f --force" -- "${cur}"))
                return
                ;;
        esac
        return
    fi

    # Handle setup subcommands
    if [[ "$cmd" == "setup" ]]; then
        case "$subcmd" in
            "")
                COMPREPLY=($(compgen -W "${setup_actions}" -- "${cur}"))
                return
                ;;
            completions)
                case "$prev" in
                    -s|--shell)
                        COMPREPLY=($(compgen -W "bash zsh fish" -- "${cur}"))
                        return
                        ;;
                esac
                COMPREPLY=($(compgen -W "-s --shell -u --user-only" -- "${cur}"))
                return
                ;;
            ssh)
                case "$prev" in
                    -t|--type)
                        COMPREPLY=($(compgen -W "ed25519 rsa ecdsa" -- "${cur}"))
                        return
                        ;;
                    -T|--test)
                        COMPREPLY=($(compgen -W "github.com gitlab.com bitbucket.org" -- "${cur}"))
                        return
                        ;;
                esac
                COMPREPLY=($(compgen -W "-g --generate -t --type -T --test -S --show" -- "${cur}"))
                return
                ;;
            init|permissions|doctor)
                return
                ;;
        esac
        return
    fi

    # Handle backup subcommands
    if [[ "$cmd" == "backup" || "$cmd" == "bak" ]]; then
        case "$subcmd" in
            "")
                COMPREPLY=($(compgen -W "${backup_actions}" -- "${cur}"))
                return
                ;;
            create|new)
                case "$prev" in
                    create|new)
                        COMPREPLY=($(compgen -W "$(_wasm_get_apps)" -- "${cur}"))
                        return
                        ;;
                    -m|--description)
                        return
                        ;;
                    -t|--tags)
                        return
                        ;;
                esac
                COMPREPLY=($(compgen -W "-m --description --no-env --include-node-modules --include-build -t --tags" -- "${cur}"))
                return
                ;;
            list|ls)
                case "$prev" in
                    list|ls)
                        COMPREPLY=($(compgen -W "$(_wasm_get_apps)" -- "${cur}"))
                        return
                        ;;
                    -t|--tags)
                        return
                        ;;
                    -n|--limit)
                        return
                        ;;
                esac
                COMPREPLY=($(compgen -W "-t --tags -n --limit --json" -- "${cur}"))
                return
                ;;
            restore)
                case "$prev" in
                    restore)
                        COMPREPLY=($(compgen -W "$(_wasm_get_backups)" -- "${cur}"))
                        return
                        ;;
                    --target-domain)
                        COMPREPLY=($(compgen -W "$(_wasm_get_apps)" -- "${cur}"))
                        return
                        ;;
                esac
                COMPREPLY=($(compgen -W "--target-domain --no-env --no-verify -f --force" -- "${cur}"))
                return
                ;;
            delete|remove|rm)
                case "$prev" in
                    delete|remove|rm)
                        COMPREPLY=($(compgen -W "$(_wasm_get_backups)" -- "${cur}"))
                        return
                        ;;
                esac
                COMPREPLY=($(compgen -W "-f --force -y" -- "${cur}"))
                return
                ;;
            verify|check|info|show)
                if [[ "$prev" == "$subcmd" ]]; then
                    COMPREPLY=($(compgen -W "$(_wasm_get_backups)" -- "${cur}"))
                    return
                fi
                if [[ "$subcmd" == "info" || "$subcmd" == "show" ]]; then
                    COMPREPLY=($(compgen -W "--json" -- "${cur}"))
                fi
                return
                ;;
            storage)
                COMPREPLY=($(compgen -W "--json" -- "${cur}"))
                return
                ;;
        esac
        return
    fi

    # Handle rollback command
    if [[ "$cmd" == "rollback" || "$cmd" == "rb" ]]; then
        case "$prev" in
            rollback|rb)
                COMPREPLY=($(compgen -W "$(_wasm_get_apps)" -- "${cur}"))
                return
                ;;
        esac
        # Check if domain was already provided (second positional argument is backup_id)
        local domain_provided=false
        for ((i=1; i < cword; i++)); do
            case "${words[i]}" in
                rollback|rb|--*) continue ;;
                *) domain_provided=true; break ;;
            esac
        done
        if $domain_provided && [[ "$prev" != "rollback" && "$prev" != "rb" ]]; then
            COMPREPLY=($(compgen -W "$(_wasm_get_backups) --no-rebuild" -- "${cur}"))
        fi
        return
    fi

    # Handle db subcommands
    if [[ "$cmd" == "db" || "$cmd" == "database" ]]; then
        case "$subcmd" in
            "")
                COMPREPLY=($(compgen -W "${db_actions}" -- "${cur}"))
                return
                ;;
            install|uninstall|start|stop|restart)
                if [[ "$prev" == "$subcmd" ]]; then
                    COMPREPLY=($(compgen -W "${db_engines}" -- "${cur}"))
                    return
                fi
                if [[ "$subcmd" == "uninstall" ]]; then
                    COMPREPLY=($(compgen -W "--purge -f --force -y" -- "${cur}"))
                fi
                return
                ;;
            status|engines)
                case "$prev" in
                    status)
                        COMPREPLY=($(compgen -W "${db_engines}" -- "${cur}"))
                        return
                        ;;
                esac
                COMPREPLY=($(compgen -W "--json" -- "${cur}"))
                return
                ;;
            create)
                case "$prev" in
                    -e|--engine)
                        COMPREPLY=($(compgen -W "${db_engines}" -- "${cur}"))
                        return
                        ;;
                    -o|--owner)
                        return
                        ;;
                    --encoding)
                        COMPREPLY=($(compgen -W "UTF8 LATIN1 SQL_ASCII" -- "${cur}"))
                        return
                        ;;
                esac
                COMPREPLY=($(compgen -W "-e --engine -o --owner --encoding" -- "${cur}"))
                return
                ;;
            drop)
                case "$prev" in
                    -e|--engine)
                        COMPREPLY=($(compgen -W "${db_engines}" -- "${cur}"))
                        return
                        ;;
                esac
                COMPREPLY=($(compgen -W "-e --engine -f --force -y" -- "${cur}"))
                return
                ;;
            list|ls)
                case "$prev" in
                    -e|--engine)
                        COMPREPLY=($(compgen -W "${db_engines}" -- "${cur}"))
                        return
                        ;;
                esac
                COMPREPLY=($(compgen -W "-e --engine --json" -- "${cur}"))
                return
                ;;
            info)
                case "$prev" in
                    -e|--engine)
                        COMPREPLY=($(compgen -W "${db_engines}" -- "${cur}"))
                        return
                        ;;
                esac
                COMPREPLY=($(compgen -W "-e --engine --json" -- "${cur}"))
                return
                ;;
            user-create)
                case "$prev" in
                    -e|--engine)
                        COMPREPLY=($(compgen -W "${db_engines}" -- "${cur}"))
                        return
                        ;;
                    -p|--password|-d|--database|--host)
                        return
                        ;;
                esac
                COMPREPLY=($(compgen -W "-e --engine -p --password -d --database --host" -- "${cur}"))
                return
                ;;
            user-delete)
                case "$prev" in
                    -e|--engine)
                        COMPREPLY=($(compgen -W "${db_engines}" -- "${cur}"))
                        return
                        ;;
                    --host)
                        return
                        ;;
                esac
                COMPREPLY=($(compgen -W "-e --engine --host -f --force -y" -- "${cur}"))
                return
                ;;
            user-list)
                case "$prev" in
                    -e|--engine)
                        COMPREPLY=($(compgen -W "${db_engines}" -- "${cur}"))
                        return
                        ;;
                esac
                COMPREPLY=($(compgen -W "-e --engine --json" -- "${cur}"))
                return
                ;;
            grant|revoke)
                case "$prev" in
                    -e|--engine)
                        COMPREPLY=($(compgen -W "${db_engines}" -- "${cur}"))
                        return
                        ;;
                    --privileges|--host)
                        return
                        ;;
                esac
                COMPREPLY=($(compgen -W "-e --engine --privileges --host" -- "${cur}"))
                return
                ;;
            backup)
                case "$prev" in
                    -e|--engine)
                        COMPREPLY=($(compgen -W "${db_engines}" -- "${cur}"))
                        return
                        ;;
                    -o|--output)
                        _filedir
                        return
                        ;;
                esac
                COMPREPLY=($(compgen -W "-e --engine -o --output --no-compress" -- "${cur}"))
                return
                ;;
            restore)
                case "$prev" in
                    -e|--engine)
                        COMPREPLY=($(compgen -W "${db_engines}" -- "${cur}"))
                        return
                        ;;
                    restore)
                        return
                        ;;
                esac
                # Check if database name was given, then complete file
                local has_db=false
                for ((i=1; i < cword; i++)); do
                    if [[ "${words[i]}" != -* && "${words[i]}" != "db" && "${words[i]}" != "database" && "${words[i]}" != "restore" ]]; then
                        has_db=true
                        break
                    fi
                done
                if $has_db && [[ "$prev" != -* ]]; then
                    _filedir
                    return
                fi
                COMPREPLY=($(compgen -W "-e --engine --drop -f --force -y" -- "${cur}"))
                return
                ;;
            backups)
                case "$prev" in
                    -e|--engine)
                        COMPREPLY=($(compgen -W "${db_engines}" -- "${cur}"))
                        return
                        ;;
                    -d|--database)
                        return
                        ;;
                esac
                COMPREPLY=($(compgen -W "-e --engine -d --database --json" -- "${cur}"))
                return
                ;;
            query)
                case "$prev" in
                    -e|--engine)
                        COMPREPLY=($(compgen -W "${db_engines}" -- "${cur}"))
                        return
                        ;;
                esac
                COMPREPLY=($(compgen -W "-e --engine" -- "${cur}"))
                return
                ;;
            connect)
                case "$prev" in
                    -e|--engine)
                        COMPREPLY=($(compgen -W "${db_engines}" -- "${cur}"))
                        return
                        ;;
                    -d|--database|-u|--username)
                        return
                        ;;
                esac
                COMPREPLY=($(compgen -W "-e --engine -d --database -u --username" -- "${cur}"))
                return
                ;;
            connection-string)
                case "$prev" in
                    -e|--engine)
                        COMPREPLY=($(compgen -W "${db_engines}" -- "${cur}"))
                        return
                        ;;
                    -p|--password|--host)
                        return
                        ;;
                esac
                COMPREPLY=($(compgen -W "-e --engine -p --password --host" -- "${cur}"))
                return
                ;;
            config)
                case "$prev" in
                    -e|--engine)
                        COMPREPLY=($(compgen -W "${db_engines}" -- "${cur}"))
                        return
                        ;;
                    -u|--user|-p|--password)
                        return
                        ;;
                esac
                COMPREPLY=($(compgen -W "-e --engine -u --user -p --password" -- "${cur}"))
                return
                ;;
        esac
        return
    fi

    # Handle web subcommands
    if [[ "$cmd" == "web" ]]; then
        case "$subcmd" in
            "")
                COMPREPLY=($(compgen -W "${web_actions}" -- "${cur}"))
                return
                ;;
            start|restart)
                case "$prev" in
                    -H|--host)
                        COMPREPLY=($(compgen -W "127.0.0.1 0.0.0.0 localhost" -- "${cur}"))
                        return
                        ;;
                    -p|--port)
                        return
                        ;;
                esac
                COMPREPLY=($(compgen -W "-H --host -p --port -d --daemon" -- "${cur}"))
                return
                ;;
            stop|status)
                return
                ;;
            token)
                COMPREPLY=($(compgen -W "-r --regenerate" -- "${cur}"))
                return
                ;;
            install)
                COMPREPLY=($(compgen -W "--apt --pip" -- "${cur}"))
                return
                ;;
        esac
        return
    fi

    # Handle store subcommands
    if [[ "$cmd" == "store" ]]; then
        case "$subcmd" in
            "")
                COMPREPLY=($(compgen -W "${store_actions}" -- "${cur}"))
                return
                ;;
            stats)
                COMPREPLY=($(compgen -W "--json" -- "${cur}"))
                return
                ;;
            export)
                case "$prev" in
                    -o|--output)
                        _filedir
                        return
                        ;;
                esac
                COMPREPLY=($(compgen -W "-o --output" -- "${cur}"))
                return
                ;;
            init|import|sync|path)
                return
                ;;
        esac
        return
    fi

    # Handle monitor subcommands
    if [[ "$cmd" == "monitor" || "$cmd" == "mon" ]]; then
        case "$subcmd" in
            "")
                COMPREPLY=($(compgen -W "${monitor_actions}" -- "${cur}"))
                return
                ;;
            scan)
                COMPREPLY=($(compgen -W "--dry-run --force-ai --all" -- "${cur}"))
                return
                ;;
            status|run|enable|install|disable|uninstall|test-email|config)
                return
                ;;
        esac
        return
    fi

    # Handle health command (no subcommands)
    if [[ "$cmd" == "health" ]]; then
        return
    fi

    # Handle webapp commands (top-level)
    case "$cmd" in
        create|new|deploy)
            case "$prev" in
                -d|--domain)
                    return
                    ;;
                -s|--source)
                    _filedir
                    return
                    ;;
                -t|--type)
                    COMPREPLY=($(compgen -W "${app_types}" -- "${cur}"))
                    return
                    ;;
                -p|--port)
                    return
                    ;;
                -w|--webserver)
                    COMPREPLY=($(compgen -W "${webservers}" -- "${cur}"))
                    return
                    ;;
                -b|--branch)
                    return
                    ;;
                --env-file)
                    _filedir
                    return
                    ;;
                --pm|--package-manager)
                    COMPREPLY=($(compgen -W "${package_managers}" -- "${cur}"))
                    return
                    ;;
            esac
            COMPREPLY=($(compgen -W "-d --domain -s --source -t --type -p --port -w --webserver -b --branch --no-ssl --env-file --pm --package-manager" -- "${cur}"))
            return
            ;;
        status|info|restart|stop|start)
            if [[ "$prev" == "$cmd" ]]; then
                COMPREPLY=($(compgen -W "$(_wasm_get_apps)" -- "${cur}"))
            fi
            return
            ;;
        update|upgrade)
            case "$prev" in
                update|upgrade)
                    COMPREPLY=($(compgen -W "$(_wasm_get_apps)" -- "${cur}"))
                    return
                    ;;
                -s|--source)
                    _filedir
                    return
                    ;;
                -b|--branch)
                    return
                    ;;
                --pm|--package-manager)
                    COMPREPLY=($(compgen -W "${package_managers}" -- "${cur}"))
                    return
                    ;;
            esac
            COMPREPLY=($(compgen -W "-s --source -b --branch --pm --package-manager" -- "${cur}"))
            return
            ;;
        delete|remove|rm)
            case "$prev" in
                delete|remove|rm)
                    COMPREPLY=($(compgen -W "$(_wasm_get_apps)" -- "${cur}"))
                    return
                    ;;
            esac
            COMPREPLY=($(compgen -W "-f --force -y --keep-files" -- "${cur}"))
            return
            ;;
        logs)
            case "$prev" in
                logs)
                    COMPREPLY=($(compgen -W "$(_wasm_get_apps)" -- "${cur}"))
                    return
                    ;;
                -n|--lines)
                    return
                    ;;
            esac
            COMPREPLY=($(compgen -W "-f --follow -n --lines" -- "${cur}"))
            return
            ;;
        list|ls)
            return
            ;;
    esac
}

# Register the completion function
complete -F _wasm_completion wasm
