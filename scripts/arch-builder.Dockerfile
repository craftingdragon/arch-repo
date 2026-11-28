FROM archlinux:base
RUN pacman -Syu --noconfirm --needed base-devel git python gnupg ccache
