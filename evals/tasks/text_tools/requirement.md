实现一个 Python 文本工具模块，要求：

1. count_words(text) 返回按空白分隔的单词数量，空字符串和纯空白返回 0
2. slugify(text) 去除首尾空白、将连续空白替换为单个连字符，并转换为小写
3. truncate(text, limit) 在文本长度超过 limit 时截取并追加 ...，否则返回原文；limit 小于 3 时抛出 ValueError
4. 提供 if __name__ == '__main__': 演示用法
