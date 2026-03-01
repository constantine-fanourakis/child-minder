# Bash tab completion for cmctl (Child Minder management utility)
# Installed to /etc/bash_completion.d/cmctl by install.sh

_cmctl_completions() {
    local cur prev commands
    COMPREPLY=()
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"

    commands="block unblock limit unlimit add-to-group remove-from-group \
group-limit group-unlimit user-limit user-weekday-limit user-weekend-limit \
user-unlimit groups add-user remove-user config usage status \
reset logs enable disable disable-user enable-user set-weekday-hours \
set-weekend-hours set-user-hours add-weekday-window add-weekend-window \
remove-weekday-window remove-weekend-window user-status"

    # Commands that take a username as their first argument
    local user_commands="add-user remove-user disable-user enable-user \
set-weekday-hours set-weekend-hours set-user-hours add-weekday-window \
add-weekend-window remove-weekday-window remove-weekend-window user-status \
user-limit user-weekday-limit user-weekend-limit user-unlimit"

    # Complete subcommand (first argument)
    if [ "$COMP_CWORD" -eq 1 ]; then
        COMPREPLY=( $(compgen -W "$commands" -- "$cur") )
        return
    fi

    local cmd="${COMP_WORDS[1]}"

    # Complete username for commands that need one
    if [ "$COMP_CWORD" -eq 2 ]; then
        for uc in $user_commands; do
            if [ "$cmd" = "$uc" ]; then
                COMPREPLY=( $(compgen -W "$(compgen -u)" -- "$cur") )
                return
            fi
        done
    fi

    # Complete flags for disable-user
    if [ "$cmd" = "disable-user" ]; then
        case "$prev" in
            -r|--reason)
                return  # User types the reason
                ;;
            -t|--hours)
                return  # User types the number
                ;;
        esac
        if [[ "$cur" == -* ]]; then
            COMPREPLY=( $(compgen -W "-r --reason -t --hours" -- "$cur") )
            return
        fi
    fi

    # Complete flags for logs
    if [ "$cmd" = "logs" ]; then
        case "$prev" in
            -n|--lines)
                return  # User types the number
                ;;
        esac
        if [[ "$cur" == -* ]]; then
            COMPREPLY=( $(compgen -W "-n --lines" -- "$cur") )
            return
        fi
    fi
}

complete -F _cmctl_completions cmctl
complete -F _cmctl_completions cmctl.py
