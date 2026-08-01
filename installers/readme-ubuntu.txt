========================================================================
Ubuntu Guide
========================================================================

>>> [ Dealing with .AppImage Files ] <<<

Step 1

Locate the .AppImage file.

Step 2

Open Terminal in the same folder.

Step 3

Make it executable: chmod +x filename.AppImage

Step 4

Run it: ./filename.AppImage

If it fails, read the error message displayed in the terminal.

------------------------------------------------------------------------

>>> [ Dealing with .tar.gz Files ] <<<

Step 1

Locate the .tar.gz file.

Step 2

Open Terminal in the same folder.

Step 3

Extract it: tar -xzf filename.tar.gz

Step 4

Open the extracted folder: cd extracted_folder

Step 5

Read any README or INSTALL file if present.

Step 6

If there is an executable: chmod +x program_name then ./program_name

Step 7

If it contains source code, follow the project’s instructions. Common
commands include:

./configure make sudo make install

or

cmake . make

or

meson setup build meson compile -C build