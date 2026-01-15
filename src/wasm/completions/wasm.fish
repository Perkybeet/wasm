# Fish completion script for WASM (Web App System Management)
#
# Installation:
#   Option 1 - System-wide (requires sudo):
#     sudo cp wasm.fish /usr/share/fish/vendor_completions.d/wasm.fish
#
#   Option 2 - User-local:
#     cp wasm.fish ~/.config/fish/completions/wasm.fish
#
#   Completions should work immediately or after: exec fish

# Disable file completions for commands
complete -c wasm -f

# Helper functions
function __wasm_get_apps
    # Try to get domains from SQLite store (fast and accurate)
    set -l db_path "/var/lib/wasm/wasm.db"
    if not test -f "$db_path"
        set db_path "$HOME/.local/share/wasm/wasm.db"
    end

    if test -f "$db_path"; and command -v sqlite3 >/dev/null
        sqlite3 "$db_path" "SELECT domain FROM apps" 2>/dev/null
        return
    end

    # Fallback: list app directories and convert names to domains
    set -l apps_dir "/var/www/apps"
    if test -d "$apps_dir"
        for app in $apps_dir/*/
            if test -d "$app"
                set -l name (basename "$app")
                # Convert wasm-example-com to example.com
                if string match -q 'wasm-*' "$name"
                    string replace 'wasm-' '' "$name" | string replace -a '-' '.'
                else
                    echo "$name"
                end
            end
        end 2>/dev/null
    end
end

function __wasm_get_services
    systemctl list-units --type=service --all 2>/dev/null | \
        grep -E '^wasm-[^ ]+\.service' | \
        sed 's/^\(wasm-[^ ]*\)\.service.*/\1/' | \
        sed 's/^wasm-//'
end

function __wasm_get_sites
    set -l nginx_dir "/etc/nginx/sites-available"
    set -l apache_dir "/etc/apache2/sites-available"

    if test -d "$nginx_dir"
        for site in $nginx_dir/*
            if test -f "$site"; and test (basename "$site") != "default"
                basename "$site"
            end
        end 2>/dev/null
    end

    if test -d "$apache_dir"
        for site in $apache_dir/*.conf
            if test -f "$site"
                basename "$site" .conf
            end
        end 2>/dev/null
    end
end

function __wasm_get_certs
    set -l live_dir "/etc/letsencrypt/live"
    if test -d "$live_dir"
        for cert in $live_dir/*/
            if test -d "$cert"
                basename "$cert"
            end
        end 2>/dev/null
    end
end

function __wasm_get_backups
    set -l backup_dir "/var/www/backups"
    if test -d "$backup_dir"
        for backup in $backup_dir/*.tar.gz
            if test -f "$backup"
                basename "$backup" .tar.gz
            end
        end 2>/dev/null
    end
end

# Check if we're at subcommand position
function __wasm_needs_command
    set -l cmd (commandline -opc)
    test (count $cmd) -eq 1
end

function __wasm_using_command
    set -l cmd (commandline -opc)
    if test (count $cmd) -ge 2
        if contains -- $cmd[2] $argv
            return 0
        end
    end
    return 1
end

function __wasm_using_subcommand
    set -l cmd (commandline -opc)
    if test (count $cmd) -ge 3
        if contains -- $cmd[2] site service svc cert ssl certificate setup backup bak db database web store monitor mon
            if contains -- $cmd[3] $argv
                return 0
            end
        end
    end
    return 1
end

# Global options
complete -c wasm -s h -l help -d 'Show help message'
complete -c wasm -s V -l version -d 'Show version'
complete -c wasm -s v -l verbose -d 'Enable verbose output'
complete -c wasm -s i -l interactive -d 'Run in interactive mode'
complete -c wasm -l no-color -d 'Disable colored output'
complete -c wasm -l dry-run -d 'Show what would be done without making changes'
complete -c wasm -l json -d 'Output results in JSON format'
complete -c wasm -l changelog -d 'Show changelog for current version'

# Main commands
complete -c wasm -n __wasm_needs_command -a create -d 'Deploy a new web application'
complete -c wasm -n __wasm_needs_command -a new -d 'Deploy a new web application'
complete -c wasm -n __wasm_needs_command -a deploy -d 'Deploy a new web application'
complete -c wasm -n __wasm_needs_command -a list -d 'List deployed applications'
complete -c wasm -n __wasm_needs_command -a ls -d 'List deployed applications'
complete -c wasm -n __wasm_needs_command -a status -d 'Show application status'
complete -c wasm -n __wasm_needs_command -a info -d 'Show application status'
complete -c wasm -n __wasm_needs_command -a restart -d 'Restart an application'
complete -c wasm -n __wasm_needs_command -a stop -d 'Stop an application'
complete -c wasm -n __wasm_needs_command -a start -d 'Start an application'
complete -c wasm -n __wasm_needs_command -a update -d 'Update an application'
complete -c wasm -n __wasm_needs_command -a upgrade -d 'Update an application'
complete -c wasm -n __wasm_needs_command -a delete -d 'Delete an application'
complete -c wasm -n __wasm_needs_command -a remove -d 'Delete an application'
complete -c wasm -n __wasm_needs_command -a rm -d 'Delete an application'
complete -c wasm -n __wasm_needs_command -a logs -d 'View application logs'
complete -c wasm -n __wasm_needs_command -a health -d 'Check system health and diagnose issues'
complete -c wasm -n __wasm_needs_command -a site -d 'Manage web server sites'
complete -c wasm -n __wasm_needs_command -a service -d 'Manage systemd services'
complete -c wasm -n __wasm_needs_command -a svc -d 'Manage systemd services'
complete -c wasm -n __wasm_needs_command -a cert -d 'Manage SSL certificates'
complete -c wasm -n __wasm_needs_command -a ssl -d 'Manage SSL certificates'
complete -c wasm -n __wasm_needs_command -a certificate -d 'Manage SSL certificates'
complete -c wasm -n __wasm_needs_command -a setup -d 'Initial setup and configuration'
complete -c wasm -n __wasm_needs_command -a backup -d 'Manage application backups'
complete -c wasm -n __wasm_needs_command -a bak -d 'Manage application backups'
complete -c wasm -n __wasm_needs_command -a rollback -d 'Rollback an application to a previous state'
complete -c wasm -n __wasm_needs_command -a rb -d 'Rollback an application to a previous state'
complete -c wasm -n __wasm_needs_command -a db -d 'Database management'
complete -c wasm -n __wasm_needs_command -a database -d 'Database management'
complete -c wasm -n __wasm_needs_command -a web -d 'Web dashboard interface'
complete -c wasm -n __wasm_needs_command -a store -d 'Manage WASM persistence store'
complete -c wasm -n __wasm_needs_command -a monitor -d 'AI-powered process security monitoring'
complete -c wasm -n __wasm_needs_command -a mon -d 'AI-powered process security monitoring'

# create/new/deploy options
complete -c wasm -n '__wasm_using_command create new deploy' -s d -l domain -d 'Target domain name'
complete -c wasm -n '__wasm_using_command create new deploy' -s s -l source -d 'Source (Git URL or local path)'
complete -c wasm -n '__wasm_using_command create new deploy' -s t -l type -xa 'nextjs nodejs vite python static auto' -d 'Application type'
complete -c wasm -n '__wasm_using_command create new deploy' -s p -l port -d 'Application port'
complete -c wasm -n '__wasm_using_command create new deploy' -s w -l webserver -xa 'nginx apache' -d 'Web server to use'
complete -c wasm -n '__wasm_using_command create new deploy' -s b -l branch -d 'Git branch to deploy'
complete -c wasm -n '__wasm_using_command create new deploy' -l no-ssl -d 'Skip SSL certificate configuration'
complete -c wasm -n '__wasm_using_command create new deploy' -l env-file -d 'Path to environment file'
complete -c wasm -n '__wasm_using_command create new deploy' -l pm -l package-manager -xa 'npm pnpm bun auto' -d 'Package manager to use'

# status/info/restart/stop/start - complete with apps
complete -c wasm -n '__wasm_using_command status info restart stop start' -xa '(__wasm_get_apps)' -d 'Application'

# update/upgrade options
complete -c wasm -n '__wasm_using_command update upgrade' -xa '(__wasm_get_apps)' -d 'Application'
complete -c wasm -n '__wasm_using_command update upgrade' -s s -l source -d 'New source URL'
complete -c wasm -n '__wasm_using_command update upgrade' -s b -l branch -d 'Git branch'
complete -c wasm -n '__wasm_using_command update upgrade' -l pm -l package-manager -xa 'npm pnpm bun auto' -d 'Package manager'

# delete/remove/rm options
complete -c wasm -n '__wasm_using_command delete remove rm' -xa '(__wasm_get_apps)' -d 'Application'
complete -c wasm -n '__wasm_using_command delete remove rm' -s f -l force -d 'Skip confirmation'
complete -c wasm -n '__wasm_using_command delete remove rm' -s y -d 'Skip confirmation'
complete -c wasm -n '__wasm_using_command delete remove rm' -l keep-files -d 'Keep application files'

# logs options
complete -c wasm -n '__wasm_using_command logs' -xa '(__wasm_get_apps)' -d 'Application'
complete -c wasm -n '__wasm_using_command logs' -s f -l follow -d 'Follow log output'
complete -c wasm -n '__wasm_using_command logs' -s n -l lines -d 'Number of lines'

# rollback options
complete -c wasm -n '__wasm_using_command rollback rb' -xa '(__wasm_get_apps)' -d 'Application'
complete -c wasm -n '__wasm_using_command rollback rb' -l no-rebuild -d 'Don\'t rebuild after restore'

# site subcommands
complete -c wasm -n '__wasm_using_command site' -a create -d 'Create a new site configuration'
complete -c wasm -n '__wasm_using_command site' -a list -d 'List all sites'
complete -c wasm -n '__wasm_using_command site' -a ls -d 'List all sites'
complete -c wasm -n '__wasm_using_command site' -a enable -d 'Enable a site'
complete -c wasm -n '__wasm_using_command site' -a disable -d 'Disable a site'
complete -c wasm -n '__wasm_using_command site' -a delete -d 'Delete a site'
complete -c wasm -n '__wasm_using_command site' -a remove -d 'Delete a site'
complete -c wasm -n '__wasm_using_command site' -a rm -d 'Delete a site'
complete -c wasm -n '__wasm_using_command site' -a show -d 'Show site configuration'
complete -c wasm -n '__wasm_using_command site' -a cat -d 'Show site configuration'

# site create options
complete -c wasm -n '__wasm_using_subcommand create; and __wasm_using_command site' -s d -l domain -d 'Domain name'
complete -c wasm -n '__wasm_using_subcommand create; and __wasm_using_command site' -s w -l webserver -xa 'nginx apache' -d 'Web server'
complete -c wasm -n '__wasm_using_subcommand create; and __wasm_using_command site' -s t -l template -xa 'proxy static' -d 'Configuration template'
complete -c wasm -n '__wasm_using_subcommand create; and __wasm_using_command site' -s p -l port -d 'Backend port'

# site list options
complete -c wasm -n '__wasm_using_subcommand list ls; and __wasm_using_command site' -s w -l webserver -xa 'nginx apache all' -d 'Filter by web server'

# site enable/disable/show/cat - complete with sites
complete -c wasm -n '__wasm_using_subcommand enable disable show cat; and __wasm_using_command site' -xa '(__wasm_get_sites)' -d 'Site'

# site delete options
complete -c wasm -n '__wasm_using_subcommand delete remove rm; and __wasm_using_command site' -xa '(__wasm_get_sites)' -d 'Site'
complete -c wasm -n '__wasm_using_subcommand delete remove rm; and __wasm_using_command site' -s f -l force -d 'Skip confirmation'

# service subcommands
complete -c wasm -n '__wasm_using_command service svc' -a create -d 'Create a new service'
complete -c wasm -n '__wasm_using_command service svc' -a list -d 'List managed services'
complete -c wasm -n '__wasm_using_command service svc' -a ls -d 'List managed services'
complete -c wasm -n '__wasm_using_command service svc' -a status -d 'Show service status'
complete -c wasm -n '__wasm_using_command service svc' -a info -d 'Show service status'
complete -c wasm -n '__wasm_using_command service svc' -a start -d 'Start a service'
complete -c wasm -n '__wasm_using_command service svc' -a stop -d 'Stop a service'
complete -c wasm -n '__wasm_using_command service svc' -a restart -d 'Restart a service'
complete -c wasm -n '__wasm_using_command service svc' -a logs -d 'View service logs'
complete -c wasm -n '__wasm_using_command service svc' -a delete -d 'Delete a service'
complete -c wasm -n '__wasm_using_command service svc' -a remove -d 'Delete a service'
complete -c wasm -n '__wasm_using_command service svc' -a rm -d 'Delete a service'

# service create options
complete -c wasm -n '__wasm_using_subcommand create; and __wasm_using_command service svc' -s n -l name -d 'Service name'
complete -c wasm -n '__wasm_using_subcommand create; and __wasm_using_command service svc' -s c -l command -d 'Command to execute'
complete -c wasm -n '__wasm_using_subcommand create; and __wasm_using_command service svc' -s d -l directory -d 'Working directory'
complete -c wasm -n '__wasm_using_subcommand create; and __wasm_using_command service svc' -s u -l user -d 'User to run as'
complete -c wasm -n '__wasm_using_subcommand create; and __wasm_using_command service svc' -l description -d 'Service description'

# service list options
complete -c wasm -n '__wasm_using_subcommand list ls; and __wasm_using_command service svc' -s a -l all -d 'Show all system services'

# service status/start/stop/restart - complete with services
complete -c wasm -n '__wasm_using_subcommand status info start stop restart; and __wasm_using_command service svc' -xa '(__wasm_get_services)' -d 'Service'

# service logs options
complete -c wasm -n '__wasm_using_subcommand logs; and __wasm_using_command service svc' -xa '(__wasm_get_services)' -d 'Service'
complete -c wasm -n '__wasm_using_subcommand logs; and __wasm_using_command service svc' -s f -l follow -d 'Follow log output'
complete -c wasm -n '__wasm_using_subcommand logs; and __wasm_using_command service svc' -s n -l lines -d 'Number of lines'

# service delete options
complete -c wasm -n '__wasm_using_subcommand delete remove rm; and __wasm_using_command service svc' -xa '(__wasm_get_services)' -d 'Service'
complete -c wasm -n '__wasm_using_subcommand delete remove rm; and __wasm_using_command service svc' -s f -l force -d 'Skip confirmation'

# cert subcommands
complete -c wasm -n '__wasm_using_command cert ssl certificate' -a create -d 'Obtain a new certificate'
complete -c wasm -n '__wasm_using_command cert ssl certificate' -a obtain -d 'Obtain a new certificate'
complete -c wasm -n '__wasm_using_command cert ssl certificate' -a new -d 'Obtain a new certificate'
complete -c wasm -n '__wasm_using_command cert ssl certificate' -a list -d 'List all certificates'
complete -c wasm -n '__wasm_using_command cert ssl certificate' -a ls -d 'List all certificates'
complete -c wasm -n '__wasm_using_command cert ssl certificate' -a info -d 'Show certificate info'
complete -c wasm -n '__wasm_using_command cert ssl certificate' -a show -d 'Show certificate info'
complete -c wasm -n '__wasm_using_command cert ssl certificate' -a renew -d 'Renew certificates'
complete -c wasm -n '__wasm_using_command cert ssl certificate' -a revoke -d 'Revoke a certificate'
complete -c wasm -n '__wasm_using_command cert ssl certificate' -a delete -d 'Delete a certificate'
complete -c wasm -n '__wasm_using_command cert ssl certificate' -a remove -d 'Delete a certificate'
complete -c wasm -n '__wasm_using_command cert ssl certificate' -a rm -d 'Delete a certificate'

# cert create options
complete -c wasm -n '__wasm_using_subcommand create obtain new; and __wasm_using_command cert ssl certificate' -s d -l domain -d 'Domain name'
complete -c wasm -n '__wasm_using_subcommand create obtain new; and __wasm_using_command cert ssl certificate' -s e -l email -d 'Email for registration'
complete -c wasm -n '__wasm_using_subcommand create obtain new; and __wasm_using_command cert ssl certificate' -s w -l webroot -d 'Webroot path'
complete -c wasm -n '__wasm_using_subcommand create obtain new; and __wasm_using_command cert ssl certificate' -l standalone -d 'Use standalone mode'
complete -c wasm -n '__wasm_using_subcommand create obtain new; and __wasm_using_command cert ssl certificate' -l nginx -d 'Use Nginx plugin'
complete -c wasm -n '__wasm_using_subcommand create obtain new; and __wasm_using_command cert ssl certificate' -l apache -d 'Use Apache plugin'
complete -c wasm -n '__wasm_using_subcommand create obtain new; and __wasm_using_command cert ssl certificate' -l dry-run -d 'Test without obtaining'

# cert info/show - complete with certs
complete -c wasm -n '__wasm_using_subcommand info show; and __wasm_using_command cert ssl certificate' -xa '(__wasm_get_certs)' -d 'Certificate'

# cert renew options
complete -c wasm -n '__wasm_using_subcommand renew; and __wasm_using_command cert ssl certificate' -s d -l domain -xa '(__wasm_get_certs)' -d 'Specific domain'
complete -c wasm -n '__wasm_using_subcommand renew; and __wasm_using_command cert ssl certificate' -l force -d 'Force renewal'
complete -c wasm -n '__wasm_using_subcommand renew; and __wasm_using_command cert ssl certificate' -l dry-run -d 'Test without renewing'

# cert revoke options
complete -c wasm -n '__wasm_using_subcommand revoke; and __wasm_using_command cert ssl certificate' -xa '(__wasm_get_certs)' -d 'Certificate'
complete -c wasm -n '__wasm_using_subcommand revoke; and __wasm_using_command cert ssl certificate' -l delete -d 'Delete after revoking'

# cert delete options
complete -c wasm -n '__wasm_using_subcommand delete remove rm; and __wasm_using_command cert ssl certificate' -xa '(__wasm_get_certs)' -d 'Certificate'
complete -c wasm -n '__wasm_using_subcommand delete remove rm; and __wasm_using_command cert ssl certificate' -s f -l force -d 'Skip confirmation'

# setup subcommands
complete -c wasm -n '__wasm_using_command setup' -a init -d 'Initialize WASM directories and configuration'
complete -c wasm -n '__wasm_using_command setup' -a completions -d 'Install shell completions'
complete -c wasm -n '__wasm_using_command setup' -a permissions -d 'Check permission status'
complete -c wasm -n '__wasm_using_command setup' -a ssh -d 'Setup SSH key for Git authentication'
complete -c wasm -n '__wasm_using_command setup' -a doctor -d 'Run system diagnostics and check for issues'

# setup completions options
complete -c wasm -n '__wasm_using_subcommand completions; and __wasm_using_command setup' -s s -l shell -xa 'bash zsh fish' -d 'Shell type'
complete -c wasm -n '__wasm_using_subcommand completions; and __wasm_using_command setup' -s u -l user-only -d 'Install for current user only'

# setup ssh options
complete -c wasm -n '__wasm_using_subcommand ssh; and __wasm_using_command setup' -s g -l generate -d 'Generate a new SSH key if none exists'
complete -c wasm -n '__wasm_using_subcommand ssh; and __wasm_using_command setup' -s t -l type -xa 'ed25519 rsa ecdsa' -d 'Type of SSH key to generate'
complete -c wasm -n '__wasm_using_subcommand ssh; and __wasm_using_command setup' -s T -l test -xa 'github.com gitlab.com bitbucket.org' -d 'Test SSH connection to a host'
complete -c wasm -n '__wasm_using_subcommand ssh; and __wasm_using_command setup' -s S -l show -d 'Show the public key'

# backup subcommands
complete -c wasm -n '__wasm_using_command backup bak' -a create -d 'Create a backup of an application'
complete -c wasm -n '__wasm_using_command backup bak' -a new -d 'Create a backup of an application'
complete -c wasm -n '__wasm_using_command backup bak' -a list -d 'List backups'
complete -c wasm -n '__wasm_using_command backup bak' -a ls -d 'List backups'
complete -c wasm -n '__wasm_using_command backup bak' -a restore -d 'Restore from a backup'
complete -c wasm -n '__wasm_using_command backup bak' -a delete -d 'Delete a backup'
complete -c wasm -n '__wasm_using_command backup bak' -a remove -d 'Delete a backup'
complete -c wasm -n '__wasm_using_command backup bak' -a rm -d 'Delete a backup'
complete -c wasm -n '__wasm_using_command backup bak' -a verify -d 'Verify a backup\'s integrity'
complete -c wasm -n '__wasm_using_command backup bak' -a check -d 'Verify a backup\'s integrity'
complete -c wasm -n '__wasm_using_command backup bak' -a info -d 'Show detailed backup information'
complete -c wasm -n '__wasm_using_command backup bak' -a show -d 'Show detailed backup information'
complete -c wasm -n '__wasm_using_command backup bak' -a storage -d 'Show backup storage usage'

# backup create options
complete -c wasm -n '__wasm_using_subcommand create new; and __wasm_using_command backup bak' -xa '(__wasm_get_apps)' -d 'Application'
complete -c wasm -n '__wasm_using_subcommand create new; and __wasm_using_command backup bak' -s m -l description -d 'Description or note for this backup'
complete -c wasm -n '__wasm_using_subcommand create new; and __wasm_using_command backup bak' -l no-env -d 'Exclude .env files from backup'
complete -c wasm -n '__wasm_using_subcommand create new; and __wasm_using_command backup bak' -l include-node-modules -d 'Include node_modules (warning: large!)'
complete -c wasm -n '__wasm_using_subcommand create new; and __wasm_using_command backup bak' -l include-build -d 'Include build artifacts'
complete -c wasm -n '__wasm_using_subcommand create new; and __wasm_using_command backup bak' -s t -l tags -d 'Comma-separated tags for the backup'

# backup list options
complete -c wasm -n '__wasm_using_subcommand list ls; and __wasm_using_command backup bak' -xa '(__wasm_get_apps)' -d 'Application'
complete -c wasm -n '__wasm_using_subcommand list ls; and __wasm_using_command backup bak' -s t -l tags -d 'Filter by tags'
complete -c wasm -n '__wasm_using_subcommand list ls; and __wasm_using_command backup bak' -s n -l limit -d 'Maximum number of backups to show'
complete -c wasm -n '__wasm_using_subcommand list ls; and __wasm_using_command backup bak' -l json -d 'Output in JSON format'

# backup restore options
complete -c wasm -n '__wasm_using_subcommand restore; and __wasm_using_command backup bak' -xa '(__wasm_get_backups)' -d 'Backup'
complete -c wasm -n '__wasm_using_subcommand restore; and __wasm_using_command backup bak' -l target-domain -xa '(__wasm_get_apps)' -d 'Restore to a different domain'
complete -c wasm -n '__wasm_using_subcommand restore; and __wasm_using_command backup bak' -l no-env -d 'Don\'t restore .env files'
complete -c wasm -n '__wasm_using_subcommand restore; and __wasm_using_command backup bak' -l no-verify -d 'Skip checksum verification'
complete -c wasm -n '__wasm_using_subcommand restore; and __wasm_using_command backup bak' -s f -l force -d 'Skip confirmation prompt'

# backup delete options
complete -c wasm -n '__wasm_using_subcommand delete remove rm; and __wasm_using_command backup bak' -xa '(__wasm_get_backups)' -d 'Backup'
complete -c wasm -n '__wasm_using_subcommand delete remove rm; and __wasm_using_command backup bak' -s f -l force -d 'Skip confirmation'

# backup verify/info options
complete -c wasm -n '__wasm_using_subcommand verify check info show; and __wasm_using_command backup bak' -xa '(__wasm_get_backups)' -d 'Backup'
complete -c wasm -n '__wasm_using_subcommand info show; and __wasm_using_command backup bak' -l json -d 'Output in JSON format'

# backup storage options
complete -c wasm -n '__wasm_using_subcommand storage; and __wasm_using_command backup bak' -l json -d 'Output in JSON format'

# db subcommands
complete -c wasm -n '__wasm_using_command db database' -a install -d 'Install a database engine'
complete -c wasm -n '__wasm_using_command db database' -a uninstall -d 'Uninstall a database engine'
complete -c wasm -n '__wasm_using_command db database' -a status -d 'Show database engine status'
complete -c wasm -n '__wasm_using_command db database' -a start -d 'Start a database engine'
complete -c wasm -n '__wasm_using_command db database' -a stop -d 'Stop a database engine'
complete -c wasm -n '__wasm_using_command db database' -a restart -d 'Restart a database engine'
complete -c wasm -n '__wasm_using_command db database' -a engines -d 'List available database engines'
complete -c wasm -n '__wasm_using_command db database' -a create -d 'Create a new database'
complete -c wasm -n '__wasm_using_command db database' -a drop -d 'Drop a database'
complete -c wasm -n '__wasm_using_command db database' -a list -d 'List databases'
complete -c wasm -n '__wasm_using_command db database' -a ls -d 'List databases'
complete -c wasm -n '__wasm_using_command db database' -a info -d 'Show database information'
complete -c wasm -n '__wasm_using_command db database' -a user-create -d 'Create a database user'
complete -c wasm -n '__wasm_using_command db database' -a user-delete -d 'Delete a database user'
complete -c wasm -n '__wasm_using_command db database' -a user-list -d 'List database users'
complete -c wasm -n '__wasm_using_command db database' -a grant -d 'Grant privileges to a user'
complete -c wasm -n '__wasm_using_command db database' -a revoke -d 'Revoke privileges from a user'
complete -c wasm -n '__wasm_using_command db database' -a backup -d 'Backup a database'
complete -c wasm -n '__wasm_using_command db database' -a restore -d 'Restore a database from backup'
complete -c wasm -n '__wasm_using_command db database' -a backups -d 'List available backups'
complete -c wasm -n '__wasm_using_command db database' -a query -d 'Execute a query'
complete -c wasm -n '__wasm_using_command db database' -a connect -d 'Connect to a database interactively'
complete -c wasm -n '__wasm_using_command db database' -a connection-string -d 'Generate a connection string'
complete -c wasm -n '__wasm_using_command db database' -a config -d 'Configure database engine credentials'

# db engine options
complete -c wasm -n '__wasm_using_subcommand install start stop restart; and __wasm_using_command db database' -xa 'mysql postgresql redis mongodb' -d 'Database engine'
complete -c wasm -n '__wasm_using_subcommand uninstall; and __wasm_using_command db database' -xa 'mysql postgresql redis mongodb' -d 'Database engine'
complete -c wasm -n '__wasm_using_subcommand uninstall; and __wasm_using_command db database' -l purge -d 'Remove all data and configuration'
complete -c wasm -n '__wasm_using_subcommand uninstall; and __wasm_using_command db database' -s f -l force -d 'Skip confirmation'
complete -c wasm -n '__wasm_using_subcommand status; and __wasm_using_command db database' -xa 'mysql postgresql redis mongodb' -d 'Database engine'
complete -c wasm -n '__wasm_using_subcommand status engines; and __wasm_using_command db database' -l json -d 'Output in JSON format'

# db create/drop/info options
complete -c wasm -n '__wasm_using_subcommand create drop info; and __wasm_using_command db database' -s e -l engine -xa 'mysql postgresql redis mongodb' -d 'Database engine'
complete -c wasm -n '__wasm_using_subcommand create; and __wasm_using_command db database' -s o -l owner -d 'Database owner (user)'
complete -c wasm -n '__wasm_using_subcommand create; and __wasm_using_command db database' -l encoding -xa 'UTF8 LATIN1 SQL_ASCII' -d 'Character encoding'
complete -c wasm -n '__wasm_using_subcommand drop; and __wasm_using_command db database' -s f -l force -d 'Skip confirmation'
complete -c wasm -n '__wasm_using_subcommand info; and __wasm_using_command db database' -l json -d 'Output in JSON format'

# db list options
complete -c wasm -n '__wasm_using_subcommand list ls; and __wasm_using_command db database' -s e -l engine -xa 'mysql postgresql redis mongodb' -d 'Database engine'
complete -c wasm -n '__wasm_using_subcommand list ls; and __wasm_using_command db database' -l json -d 'Output in JSON format'

# db user options
complete -c wasm -n '__wasm_using_subcommand user-create user-delete user-list; and __wasm_using_command db database' -s e -l engine -xa 'mysql postgresql redis mongodb' -d 'Database engine'
complete -c wasm -n '__wasm_using_subcommand user-create; and __wasm_using_command db database' -s p -l password -d 'Password (generated if not provided)'
complete -c wasm -n '__wasm_using_subcommand user-create; and __wasm_using_command db database' -s d -l database -d 'Grant access to this database'
complete -c wasm -n '__wasm_using_subcommand user-create user-delete; and __wasm_using_command db database' -l host -d 'Host restriction'
complete -c wasm -n '__wasm_using_subcommand user-delete; and __wasm_using_command db database' -s f -l force -d 'Skip confirmation'
complete -c wasm -n '__wasm_using_subcommand user-list; and __wasm_using_command db database' -l json -d 'Output in JSON format'

# db grant/revoke options
complete -c wasm -n '__wasm_using_subcommand grant revoke; and __wasm_using_command db database' -s e -l engine -xa 'mysql postgresql redis mongodb' -d 'Database engine'
complete -c wasm -n '__wasm_using_subcommand grant revoke; and __wasm_using_command db database' -l privileges -d 'Comma-separated list of privileges'
complete -c wasm -n '__wasm_using_subcommand grant revoke; and __wasm_using_command db database' -l host -d 'Host restriction'

# db backup/restore options
complete -c wasm -n '__wasm_using_subcommand backup restore; and __wasm_using_command db database' -s e -l engine -xa 'mysql postgresql redis mongodb' -d 'Database engine'
complete -c wasm -n '__wasm_using_subcommand backup; and __wasm_using_command db database' -s o -l output -d 'Output file path'
complete -c wasm -n '__wasm_using_subcommand backup; and __wasm_using_command db database' -l no-compress -d 'Don\'t compress the backup'
complete -c wasm -n '__wasm_using_subcommand restore; and __wasm_using_command db database' -l drop -d 'Drop existing database before restore'
complete -c wasm -n '__wasm_using_subcommand restore; and __wasm_using_command db database' -s f -l force -d 'Skip confirmation'

# db backups options
complete -c wasm -n '__wasm_using_subcommand backups; and __wasm_using_command db database' -s e -l engine -xa 'mysql postgresql redis mongodb' -d 'Database engine'
complete -c wasm -n '__wasm_using_subcommand backups; and __wasm_using_command db database' -s d -l database -d 'Filter by database name'
complete -c wasm -n '__wasm_using_subcommand backups; and __wasm_using_command db database' -l json -d 'Output in JSON format'

# db query/connect options
complete -c wasm -n '__wasm_using_subcommand query connect; and __wasm_using_command db database' -s e -l engine -xa 'mysql postgresql redis mongodb' -d 'Database engine'
complete -c wasm -n '__wasm_using_subcommand connect; and __wasm_using_command db database' -s d -l database -d 'Database name'
complete -c wasm -n '__wasm_using_subcommand connect; and __wasm_using_command db database' -s u -l username -d 'Username'

# db connection-string options
complete -c wasm -n '__wasm_using_subcommand connection-string; and __wasm_using_command db database' -s e -l engine -xa 'mysql postgresql redis mongodb' -d 'Database engine'
complete -c wasm -n '__wasm_using_subcommand connection-string; and __wasm_using_command db database' -s p -l password -d 'Password'
complete -c wasm -n '__wasm_using_subcommand connection-string; and __wasm_using_command db database' -l host -d 'Host'

# db config options
complete -c wasm -n '__wasm_using_subcommand config; and __wasm_using_command db database' -s e -l engine -xa 'mysql postgresql redis mongodb' -d 'Database engine'
complete -c wasm -n '__wasm_using_subcommand config; and __wasm_using_command db database' -s u -l user -d 'Admin username'
complete -c wasm -n '__wasm_using_subcommand config; and __wasm_using_command db database' -s p -l password -d 'Admin password'

# web subcommands
complete -c wasm -n '__wasm_using_command web' -a start -d 'Start the web dashboard server'
complete -c wasm -n '__wasm_using_command web' -a stop -d 'Stop the web dashboard server'
complete -c wasm -n '__wasm_using_command web' -a status -d 'Show web dashboard status'
complete -c wasm -n '__wasm_using_command web' -a restart -d 'Restart the web dashboard server'
complete -c wasm -n '__wasm_using_command web' -a token -d 'Manage access tokens'
complete -c wasm -n '__wasm_using_command web' -a install -d 'Install web dashboard dependencies'

# web start/restart options
complete -c wasm -n '__wasm_using_subcommand start restart; and __wasm_using_command web' -s H -l host -xa '127.0.0.1 0.0.0.0 localhost' -d 'Host to bind to'
complete -c wasm -n '__wasm_using_subcommand start restart; and __wasm_using_command web' -s p -l port -d 'Port to listen on'
complete -c wasm -n '__wasm_using_subcommand start restart; and __wasm_using_command web' -s d -l daemon -d 'Run in background as daemon'

# web token options
complete -c wasm -n '__wasm_using_subcommand token; and __wasm_using_command web' -s r -l regenerate -d 'Generate a new access token'

# web install options
complete -c wasm -n '__wasm_using_subcommand install; and __wasm_using_command web' -l apt -d 'Use apt to install system packages'
complete -c wasm -n '__wasm_using_subcommand install; and __wasm_using_command web' -l pip -d 'Use pip to install user packages'

# store subcommands
complete -c wasm -n '__wasm_using_command store' -a init -d 'Initialize or reinitialize the store database'
complete -c wasm -n '__wasm_using_command store' -a stats -d 'Show store statistics'
complete -c wasm -n '__wasm_using_command store' -a import -d 'Import legacy apps from systemd services and nginx configs'
complete -c wasm -n '__wasm_using_command store' -a export -d 'Export store data to JSON'
complete -c wasm -n '__wasm_using_command store' -a sync -d 'Sync store with actual systemd service states'
complete -c wasm -n '__wasm_using_command store' -a path -d 'Show the database file path'

# store stats options
complete -c wasm -n '__wasm_using_subcommand stats; and __wasm_using_command store' -l json -d 'Output as JSON'

# store export options
complete -c wasm -n '__wasm_using_subcommand export; and __wasm_using_command store' -s o -l output -d 'Output file (stdout if not specified)'

# monitor subcommands
complete -c wasm -n '__wasm_using_command monitor mon' -a status -d 'Show monitor service status'
complete -c wasm -n '__wasm_using_command monitor mon' -a scan -d 'Run a single security scan'
complete -c wasm -n '__wasm_using_command monitor mon' -a run -d 'Run monitor continuously (foreground)'
complete -c wasm -n '__wasm_using_command monitor mon' -a enable -d 'Enable monitor (installs dependencies and service if needed)'
complete -c wasm -n '__wasm_using_command monitor mon' -a install -d 'Install monitor service only (without enabling)'
complete -c wasm -n '__wasm_using_command monitor mon' -a disable -d 'Disable and stop monitor service'
complete -c wasm -n '__wasm_using_command monitor mon' -a uninstall -d 'Uninstall monitor service'
complete -c wasm -n '__wasm_using_command monitor mon' -a test-email -d 'Send a test email to verify notification settings'
complete -c wasm -n '__wasm_using_command monitor mon' -a config -d 'Show current monitor configuration'

# monitor scan options
complete -c wasm -n '__wasm_using_subcommand scan; and __wasm_using_command monitor mon' -l dry-run -d 'Don\'t terminate processes, just report'
complete -c wasm -n '__wasm_using_subcommand scan; and __wasm_using_command monitor mon' -l force-ai -d 'Force AI analysis even if no suspicious processes are found'
complete -c wasm -n '__wasm_using_subcommand scan; and __wasm_using_command monitor mon' -l all -d 'Analyze ALL processes with AI (expensive, use sparingly)'
