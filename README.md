Disclaimer: This repository contains AI-Generated code.
Reason: This tool is really just for me, I'm only releasing it publically because I feel like its a useful tool.

# **NOTE: THIS ONLY WORKS WITH PRISM LAUNCHER. NO OTHER LAUNCHER WILL WORK WITH THIS.**

A basic Minecraft (Prism Launcher) backup/restore utility for devices like the Steam Deck or Steam Machine.

Dependencies:
rclone
tkinter
python3
Prism Launcher

Tested/Works on the following Operating Systems:
Windows 11 25H2
Arch Linux
SteamOS
Bazzite
Note: For linux, both flatpak and non-flatpak versions of Prism Launcher were used.

Untested/Known to not work on the following Operating Systems:
MacOS (any version)

Rclone Setup:
You have to configure rclone yourself. Currently only tested with Google Drive and USB backup.
The cloud storage must be named "gdrive" and rclone must have full access to it.

This doesn't interact with the game files in anyway other than copying and pasting them onto google drive. This should probably work with any version of minecraft java. I've only tested it on the latest version as of writing, though.

Image:
<img width="1151" height="810" alt="image" src="https://github.com/user-attachments/assets/30d0d1e8-9962-4058-92be-2174a506e4a0" />

