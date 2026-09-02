# Use bundled rules so linting needs no cloud access or plugin download.
plugin "terraform" {
  enabled = true
  preset  = "recommended"
}
