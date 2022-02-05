WIKI
====

Запуск
------
* В /etc/hosts добавить строчки
```
127.0.0.1       www.localhost
127.0.0.1       admin.localhost
127.0.0.1       vanek.localhost
```

* В main.py заменить строку
```
domain = 'localhost'
```

* Выполнить

```
dev_appserver.py wiki-ee/

```

Установка
---------
```
appcfg.py update wiki-ee/

```

TODO
----
1. add creole lib
2. add tests
3. add templates