# vhs docs/demo/demo.tape  (brew install vhs)
Output docs/demo.gif
Set Shell "bash"
Set FontSize 15
Set Width 1240
Set Height 760
Set Padding 18
Set Theme "Catppuccin Mocha"
Set TypingSpeed 35ms

Hide
Type "PS1='$ ' source docs/demo/setup.sh" Enter
Sleep 1.5s
Show

Type "lightsout check" Enter
Sleep 2.5s
Type "at 22:40; lightsout tick" Enter
Sleep 2.5s
Type "calls_end   # call over" Enter
Sleep 0.5s
Type "at 23:05; lightsout tick" Enter
Sleep 9s
Type "at 01:10; lightsout tick" Enter
Sleep 4s
Type "clear; at 07:35; lightsout report" Enter
Sleep 7s
