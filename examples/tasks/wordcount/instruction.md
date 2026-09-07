You are in a Linux shell. Create the file `/app/solution.py` (create the `/app`
directory if it does not exist).

It must define a function:

    top_words(text: str) -> list[str]

that returns the 3 most frequent words in `text`. A "word" is a maximal run of
alphanumeric characters, compared case-insensitively (lowercase the words).
Order the result most-frequent first; break ties alphabetically. Each element is
formatted as `"word:count"`.

Example:

    top_words("the cat the dog the bird cat") == ["the:3", "cat:2", "bird:1"]
